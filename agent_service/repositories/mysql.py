"""MySQL 访问层：双连接池 + 幂等迁移器。

C2 定稿后存储收敛到单一 MySQL，按「表域」分工：
- **rw 池**（`MYSQL_DSN`）：本服务自有域 —— 组件资产、回合元数据、模型配置；需要 DDL 权限。
- **ro 池**（`MYSQL_RO_DSN`）：会话域 —— `conversations` / `messages`；**只读**，本服务永不写。

两个池分开的意义：生产可以给只读账号，代码层面也就无法误写会话域。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlsplit

from asyncmy import create_pool
from asyncmy.cursors import DictCursor

from ..core.config import settings
from ..core.errors import ConfigInvalid
from ..core.logging import get_logger

logger = get_logger("mysql")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR(191) NOT NULL,
    applied_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def _pool_kwargs(dsn: str) -> dict[str, Any]:
    parts = urlsplit(dsn)
    database = parts.path.lstrip("/")
    if not parts.hostname or not database:
        raise ConfigInvalid("MySQL DSN 缺少主机名或数据库名", detail=f"dsn={dsn.split('@')[-1]}")
    return {
        "host": parts.hostname,
        "port": parts.port or 3306,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "db": database,
        "charset": "utf8mb4",
        "autocommit": False,
    }


def split_statements(sql_text: str) -> list[str]:
    """把迁移文件切成单条语句。

    只支持本项目用到的简单 DDL：`--` 行注释 + 以 `;` 结尾的语句（不含存储过程/触发器）。
    """
    lines: list[str] = []
    for raw in sql_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(raw)
    statements = []
    for chunk in "\n".join(lines).split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements


class MySQL:
    """连接池与迁移器。进程内单例（见文件末尾 `mysql`）。"""

    def __init__(self) -> None:
        self._rw: Any = None
        self._ro: Any = None
        self._shares_pool = False

    # ── 生命周期 ──
    async def open(self) -> None:
        self._rw = await create_pool(
            minsize=settings.MYSQL_MIN_SIZE,
            maxsize=settings.MYSQL_MAX_SIZE,
            pool_recycle=settings.MYSQL_POOL_RECYCLE,
            **_pool_kwargs(settings.MYSQL_DSN),
        )
        if settings.MYSQL_RO_DSN == settings.MYSQL_DSN:
            # 本地开发常见：同一个账号。生产必须给独立只读账号（config 会告警）
            self._ro = self._rw
            self._shares_pool = True
        else:
            self._ro = await create_pool(
                minsize=settings.MYSQL_MIN_SIZE,
                maxsize=settings.MYSQL_MAX_SIZE,
                pool_recycle=settings.MYSQL_POOL_RECYCLE,
                **_pool_kwargs(settings.MYSQL_RO_DSN),
            )
        logger.info(
            "MySQL 连接池就绪（rw=%s ro=%s%s）",
            _pool_kwargs(settings.MYSQL_DSN)["db"],
            _pool_kwargs(settings.MYSQL_RO_DSN)["db"],
            "，共用同一账号" if self._shares_pool else "，独立只读账号",
        )

    async def close(self) -> None:
        for pool in {id(self._rw): self._rw, id(self._ro): self._ro}.values():
            if pool is None:
                continue
            try:
                pool.close()
                await pool.wait_closed()
            except Exception as exc:  # 关闭失败不应阻塞退出
                logger.warning("关闭连接池时出错：%s", exc)
        self._rw = self._ro = None

    # ── 上下文 ──
    @asynccontextmanager
    async def _cursor(self, pool: Any) -> AsyncIterator[tuple[Any, Any]]:
        conn = await pool.acquire()
        cursor = conn.cursor(DictCursor)
        try:
            yield conn, cursor
        finally:
            try:
                await cursor.close()
            finally:
                await pool.release(conn)

    @asynccontextmanager
    async def rw(self) -> AsyncIterator[tuple[Any, Any]]:
        """自有域（可读写）。"""
        async with self._cursor(self._rw) as pair:
            yield pair

    @asynccontextmanager
    async def ro(self) -> AsyncIterator[tuple[Any, Any]]:
        """会话域（只读）。"""
        async with self._cursor(self._ro) as pair:
            yield pair

    # ── 迁移 ──
    async def migrate(self) -> list[str]:
        """按文件名顺序执行未应用的迁移，返回本次应用的版本列表（幂等）。"""
        applied: list[str] = []
        async with self.rw() as (conn, cursor):
            # 先探存在性再建表，避免每次启动都抛出 "Table already exists" 提示
            await cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'schema_migrations' LIMIT 1"
            )
            if await cursor.fetchone() is None:
                for statement in split_statements(_SCHEMA_MIGRATIONS_SQL):
                    await cursor.execute(statement)
                await conn.commit()

            await cursor.execute("SELECT version FROM schema_migrations")
            done = {row["version"] for row in await cursor.fetchall()}

            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = path.name
                if version in done:
                    continue
                statements = split_statements(path.read_text(encoding="utf-8"))
                for statement in statements:
                    await cursor.execute(statement)
                await cursor.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
                await conn.commit()
                applied.append(version)
                logger.info("已应用迁移 %s（%d 条语句）", version, len(statements))

        if applied:
            logger.info("迁移完成，本次应用 %d 个：%s", len(applied), ", ".join(applied))
        else:
            logger.info("数据库结构已是最新，无需迁移")
        return applied

    # ── 健康检查 ──
    async def ping(self) -> dict[str, Any]:
        """真实探活：分别对两个池执行 SELECT 1。"""
        result: dict[str, Any] = {}
        for name, pool in (("rw", self._rw), ("ro", self._ro)):
            if pool is None:
                result[name] = "uninitialized"
                continue
            try:
                async with self._cursor(pool) as (_conn, cursor):
                    await cursor.execute("SELECT 1 AS ok")
                    await cursor.fetchone()
                result[name] = "ok"
            except Exception as exc:
                result[name] = f"error: {type(exc).__name__}"
                logger.warning("MySQL %s 池探活失败：%s", name, exc)
        return result

    async def table_exists(self, table: str) -> bool:
        async with self.rw() as (_conn, cursor):
            await cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s LIMIT 1",
                (table,),
            )
            return await cursor.fetchone() is not None


mysql = MySQL()
