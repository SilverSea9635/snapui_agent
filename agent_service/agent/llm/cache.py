"""Bounded LRU/TTL client cache with reference-counted request leases.

Eviction removes reuse eligibility immediately. Connections are closed only
once the last active lease releases the retired entry.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Iterator, Optional

from langchain_core.language_models.chat_models import BaseChatModel

from ...core.logging import get_logger
from .factory import ModelSpec, build_chat_model

logger = get_logger("llm.cache")
DEFAULT_MAX_ENTRIES = 64
DEFAULT_TTL_SECONDS = 1800.0


@dataclass(eq=False)
class _Entry:
    model: BaseChatModel
    created_at: float
    users: int = 0
    retired: bool = False
    disposing: bool = False
    loop: Optional[asyncio.AbstractEventLoop] = None


class ModelCache:
    """Reuse clients through ``with cache.acquire(spec) as model`` only."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("max_entries and ttl_seconds must be positive")
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = RLock()
        self._closing_tasks: set[asyncio.Task] = set()
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds

    def _retire(self, entry: _Entry, ready: list[_Entry]) -> None:
        # Called only while holding the bookkeeping lock.
        entry.retired = True
        if entry.users == 0 and not entry.disposing:
            entry.disposing = True
            ready.append(entry)

    def _expire(self, now: float, ready: list[_Entry]) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.created_at >= self.ttl_seconds:
                del self._entries[key]
                self._retire(entry, ready)

    @contextmanager
    def acquire(self, spec: ModelSpec) -> Iterator[BaseChatModel]:
        """Pin a client for the entire request, including suspended streaming."""
        key = spec.cache_key()
        ready: list[_Entry] = []
        try:
            with self._lock:
                self._expire(time.monotonic(), ready)
                entry = self._entries.get(key)
                if entry is None:
                    # Serialize construction as well: same-key callers share one client.
                    model = build_chat_model(spec)
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    entry = _Entry(model, time.monotonic(), loop=loop)
                    self._entries[key] = entry
                self._entries.move_to_end(key)
                entry.users += 1
                while len(self._entries) > self.max_entries:
                    self._retire(self._entries.popitem(last=False)[1], ready)
        finally:
            self._dispose_all(ready)

        try:
            yield entry.model
        finally:
            ready = []
            with self._lock:
                entry.users -= 1
                expired = time.monotonic() - entry.created_at >= self.ttl_seconds
                if expired and self._entries.get(key) is entry:
                    del self._entries[key]
                if entry.retired or expired:
                    self._retire(entry, ready)
            self._dispose_all(ready)

    def _dispose_all(self, entries: list[_Entry]) -> None:
        # Never close connections while holding the bookkeeping lock.
        for entry in entries:
            self._dispose(entry)

    def _dispose(self, entry: _Entry) -> None:
        sync_client = getattr(entry.model, "http_client", None)
        async_client = getattr(entry.model, "http_async_client", None)
        if sync_client is not None:
            try:
                sync_client.close()
            except Exception as exc:
                logger.warning("Model sync client cleanup failed: %s", type(exc).__name__)
        if async_client is None:
            return

        async def close_async() -> None:
            try:
                await async_client.aclose()
            except Exception as exc:
                logger.warning("Model async client cleanup failed: %s", type(exc).__name__)

        def forget(task: asyncio.Task) -> None:
            with self._lock:
                self._closing_tasks.discard(task)

        def schedule() -> None:
            task = asyncio.create_task(close_async())
            with self._lock:
                self._closing_tasks.add(task)
            task.add_done_callback(forget)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        # A lease may be released by a different thread. Keep async cleanup on
        # the client's original event loop while that loop is still running.
        if entry.loop is not None and entry.loop.is_running() and entry.loop is not loop:
            entry.loop.call_soon_threadsafe(schedule)
        elif loop is not None:
            schedule()
        else:
            asyncio.run(close_async())

    def clear(self) -> None:
        """Invalidate all reusable entries; active leases retain their clients."""
        ready: list[_Entry] = []
        with self._lock:
            for entry in self._entries.values():
                self._retire(entry, ready)
            self._entries.clear()
        self._dispose_all(ready)

    def size(self) -> int:
        """Number of reusable entries; retired active clients are not counted."""
        with self._lock:
            return len(self._entries)


_cache: Optional[ModelCache] = None
_cache_lock = Lock()


def get_model_cache() -> ModelCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = ModelCache()
        return _cache
