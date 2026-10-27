"""PostgreSQL 检查点连接池及跨进程会话锁。"""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..core.config import Settings
from ..core.errors import AgentStorageError, ConversationBusy
from ..core.logging import get_logger
from .messages import MessageRepository, SCHEMA_SQL

logger = get_logger("checkpoints")


def lock_key(value: str) -> int:
    """稳定映射到 PostgreSQL 的有符号 bigint 锁键。"""
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


class CheckpointStore:
    def __init__(self, pool: AsyncConnectionPool, *, prune: bool = True) -> None:
        self.pool = pool
        self.saver = AsyncPostgresSaver(pool)
        self.messages = MessageRepository(pool)
        self.memory = AsyncPostgresStore(pool)
        self.prune = prune
        # 始终预留一个连接供检查点读写，避免运行中的会话锁耗尽连接池。
        self._slots = asyncio.Semaphore(pool.max_size - 1)

    @asynccontextmanager
    async def lease(self, thread_id: str) -> AsyncIterator[None]:
        async with self._slots, self.pool.connection() as conn:
            key = lock_key(thread_id)
            row = await (await conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,))).fetchone()
            if not row or not row["acquired"]:
                raise ConversationBusy()
            try:
                yield
            finally:
                try:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                except BaseException:
                    # 解锁失败时销毁连接，不能把仍持有会话锁的连接放回池中。
                    await conn.close()
                    raise

    async def compact(self, thread_id: str) -> None:
        """只保留最新检查点；消息状态仍完整保存在该检查点中。"""
        if self.prune:
            await self.saver.aprune([thread_id], strategy="keep_latest")

    async def ping(self) -> dict:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT 1 AS ok")).fetchone()
        return {"ok": bool(row and row["ok"] == 1), "backend": "postgres"}


@asynccontextmanager
async def open_checkpoint_store(config: Settings) -> AsyncIterator[CheckpointStore]:
    """初始化私有 schema；表结构版本由 LangGraph 的迁移器维护。"""
    params = conninfo_to_dict(config.POSTGRES_DSN)
    kwargs = {
        "autocommit": True,
        "row_factory": dict_row,
        "prepare_threshold": None,
        "connect_timeout": config.POSTGRES_CONNECT_TIMEOUT,
        "sslmode": params.get("sslmode", "require"),
    }
    schema = sql.Identifier(config.POSTGRES_SCHEMA)

    async def configure(conn: AsyncConnection) -> None:
        await conn.execute(sql.SQL("SET search_path TO {}").format(schema))

    if config.POSTGRES_AUTO_SETUP:
        async with await AsyncConnection.connect(config.POSTGRES_DSN, **kwargs) as conn:
            key = lock_key("checkpoint-schema:" + config.POSTGRES_SCHEMA)
            await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
                await conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema))
                roles = await (await conn.execute("SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")).fetchall()
                for role in roles:
                    await conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(schema, sql.Identifier(role["rolname"])))
                await configure(conn)
                await AsyncPostgresSaver(conn).setup()
                await AsyncPostgresStore(conn).setup()
                await conn.execute(SCHEMA_SQL)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

    pool = AsyncConnectionPool(
        config.POSTGRES_DSN, kwargs=kwargs, configure=configure,
        min_size=1, max_size=config.POSTGRES_POOL_MAX_SIZE, open=False,
        timeout=config.POSTGRES_CONNECT_TIMEOUT,
        reconnect_timeout=config.POSTGRES_CONNECT_TIMEOUT,
        name="agent-checkpoints",
    )
    try:
        await pool.open(wait=True, timeout=config.POSTGRES_CONNECT_TIMEOUT)
        store = CheckpointStore(pool, prune=config.POSTGRES_PRUNE_CHECKPOINTS)
        await store.ping()
        yield store
    except (ConversationBusy, AgentStorageError):
        raise
    except Exception as exc:
        logger.error("PostgreSQL 会话存储失败：type=%s", type(exc).__name__)
        raise AgentStorageError() from None
    finally:
        await pool.close()
