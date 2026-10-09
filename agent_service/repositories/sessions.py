"""会话域（`conversations` / `messages`）只读访问。

C2 定稿：会话域的真相源在 OCRServer，本服务**只读**（用 ro 池）。
- 不提供任何写方法；删除类操作由 API 层直接抛 `SessionDomainReadOnly`。
- `conversations.user_id` / `messages.user_id` 在 OCRServer 侧是 `INT`（外键指向 `users.id`），
  因此这里统一把「十进制字符串」的用户标识转成 int 再查询。
- `created_at` 是 MySQL `DATETIME`（无时区），存的是**本地墙钟**；读取后按
  `LEGACY_DB_TIMEZONE` 解释再转 UTC，保证与 OCRServer 接口输出的 ISO-8601 UTC 一致。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.config import settings
from ..core.errors import BadRequest
from ..core.logging import get_logger
from .mysql import mysql

logger = get_logger("sessions")

_HISTORY_MAX_ROWS = 200


def _tz() -> timezone | ZoneInfo:
    try:
        return ZoneInfo(settings.LEGACY_DB_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "时区 %s 不可用，回落 UTC（时间可能与 OCRServer 相差 8 小时）",
            settings.LEGACY_DB_TIMEZONE,
        )
        return timezone.utc


def to_utc_iso(value: Any) -> str:
    """把 OCRServer 的 DATETIME（本地墙钟）转成 ISO-8601 UTC 字符串。

    刻意保留毫秒（`...:10.000Z`）——OCRServer 侧是 JS `Date.toISOString()`，
    精度与格式保持一致，前端两个来源的时间才不会看起来不同。
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=_tz())
        utc = moment.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    return str(value) if value is not None else ""


def _uid_int(user_id: str) -> int:
    value = str(user_id).strip()
    if not value.isdigit():
        raise BadRequest(
            "用户标识必须是 users.id 的十进制字符串（本服务已禁止匿名标识）",
            detail=f"userId={value!r}",
        )
    return int(value)


def _parse_content(content: Any) -> Any:
    """content 若是 JSON 串则还原为结构体（与旧行为一致）。"""
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
    return content


class SessionRepository:
    """会话域只读仓储。"""

    async def conversation_exists(self, conversation_id: str, user_id: str) -> bool:
        async with mysql.ro() as (_conn, cursor):
            await cursor.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = %s AND user_id = %s LIMIT 1",
                (conversation_id, _uid_int(user_id)),
            )
            return await cursor.fetchone() is not None

    async def list_conversations(
        self, user_id: str, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        uid = _uid_int(user_id)
        offset = (max(page, 1) - 1) * page_size
        async with mysql.ro() as (_conn, cursor):
            await cursor.execute(
                "SELECT COUNT(*) AS total FROM conversations WHERE user_id = %s", (uid,)
            )
            total = int((await cursor.fetchone() or {}).get("total", 0))

            await cursor.execute(
                "SELECT conversation_id, title, created_at FROM conversations "
                "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (uid, page_size, offset),
            )
            rows = await cursor.fetchall()

        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "conversations": [
                {
                    "id": row["conversation_id"],
                    "title": row["title"] or "",
                    "createdAt": to_utc_iso(row["created_at"]),
                }
                for row in rows
            ],
        }

    async def load_conversation(self, conversation_id: str, user_id: str) -> Optional[dict]:
        uid = _uid_int(user_id)
        async with mysql.ro() as (_conn, cursor):
            await cursor.execute(
                "SELECT conversation_id, title, created_at FROM conversations "
                "WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, uid),
            )
            conversation = await cursor.fetchone()
            if conversation is None:
                return None

            await cursor.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE conversation_id = %s AND user_id = %s ORDER BY id ASC LIMIT %s",
                (conversation_id, uid, _HISTORY_MAX_ROWS),
            )
            rows = await cursor.fetchall()

        return {
            "conversationId": conversation["conversation_id"],
            "title": conversation["title"] or "",
            "createdAt": to_utc_iso(conversation["created_at"]),
            "messages": [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": _parse_content(row["content"]),
                    "createdAt": to_utc_iso(row["created_at"]),
                }
                for row in rows
            ],
        }

    async def load_history(
        self, conversation_id: str, user_id: str, limit: Optional[int] = None
    ) -> list[dict]:
        """按时间正序返回消息（用于组装模型上下文）。"""
        uid = _uid_int(user_id)
        async with mysql.ro() as (_conn, cursor):
            if limit:
                # 取最近 limit 条后仍按正序返回
                await cursor.execute(
                    "SELECT role, content FROM ("
                    "  SELECT id, role, content FROM messages "
                    "  WHERE conversation_id = %s AND user_id = %s ORDER BY id DESC LIMIT %s"
                    ") AS recent ORDER BY id ASC",
                    (conversation_id, uid, limit),
                )
            else:
                await cursor.execute(
                    "SELECT role, content FROM messages "
                    "WHERE conversation_id = %s AND user_id = %s ORDER BY id ASC LIMIT %s",
                    (conversation_id, uid, _HISTORY_MAX_ROWS),
                )
            rows = await cursor.fetchall()

        return [{"role": row["role"], "content": _parse_content(row["content"])} for row in rows]


session_repo = SessionRepository()
