"""模型实例缓存（有界 + TTL）。

替换旧实现 `SnapUIAgent._models`：那是个**无上限、且只按模型名做 key** 的 dict。
引入用户自带模型后，那个写法会导致两个问题：
1. 内存无界增长；
2. 同名模型、不同 BaseURL/Key 的实例互相串用（安全与正确性双重问题）。

这里以 `ModelSpec.cache_key()`（含 base_url 与 api_key 哈希）为键，LRU + TTL 淘汰。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel

from ...core.logging import get_logger
from .factory import ModelSpec, build_chat_model

logger = get_logger("llm.cache")

DEFAULT_MAX_ENTRIES = 64
DEFAULT_TTL_SECONDS = 1800.0


class ModelCache:
    """LRU + TTL 的模型实例缓存。"""

    def __init__(
        self, max_entries: int = DEFAULT_MAX_ENTRIES, ttl_seconds: float = DEFAULT_TTL_SECONDS
    ) -> None:
        self._entries: "OrderedDict[str, tuple[BaseChatModel, float]]" = OrderedDict()
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds

    def get(self, spec: ModelSpec) -> BaseChatModel:
        key = spec.cache_key()
        now = time.monotonic()

        cached = self._entries.get(key)
        if cached is not None:
            model, created_at = cached
            if now - created_at < self.ttl_seconds:
                self._entries.move_to_end(key)
                return model
            # 过期：丢弃后重建（同时也让旧密钥尽早离开内存）
            self._entries.pop(key, None)

        model = build_chat_model(spec)
        self._entries[key] = (model, now)
        self._evict()
        logger.info(
            "构造模型实例 %s（缓存 %d/%d）", spec.safe_label(), len(self._entries), self.max_entries
        )
        return model

    def _evict(self) -> None:
        now = time.monotonic()
        # 先清过期
        for key in [k for k, (_m, ts) in self._entries.items() if now - ts >= self.ttl_seconds]:
            self._entries.pop(key, None)
        # 再按 LRU 淘汰
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def size(self) -> int:
        return len(self._entries)


_cache: Optional[ModelCache] = None


def get_model_cache() -> ModelCache:
    global _cache
    if _cache is None:
        _cache = ModelCache()
    return _cache
