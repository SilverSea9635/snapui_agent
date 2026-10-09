"""回合级元数据（本服务自有域）。

替代「给 OCRServer 的 messages 表加列」的做法（C2 禁令：不跨域写）。
写入失败**绝不影响对话**——它只是计量与后续 P2（附件/工具轨迹）的载体。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..core.logging import get_logger
from .mysql import mysql

logger = get_logger("turns")

_INSERT_SQL = """
INSERT INTO conversation_turns
    (conversation_id, turn_index, user_id, attachments, tool_trace, generation_mode, model)
SELECT %s,
       COALESCE(MAX(turn_index), 0) + 1,
       %s, %s, %s, %s, %s
FROM conversation_turns
WHERE conversation_id = %s
"""


async def record_turn(
    conversation_id: str,
    user_id: str,
    *,
    generation_mode: str = "fresh",
    model: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
    tool_trace: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """记录一轮对话的元数据，返回是否写入成功。"""
    payload = (
        conversation_id,
        user_id,
        json.dumps(attachments or [], ensure_ascii=False),
        json.dumps(tool_trace or [], ensure_ascii=False),
        generation_mode,
        model,
        conversation_id,
    )
    try:
        async with mysql.rw() as (conn, cursor):
            await cursor.execute(_INSERT_SQL, payload)
            await conn.commit()
        return True
    except Exception as exc:  # 计量失败不阻塞对话
        logger.warning("回合元数据写入失败（不影响本次对话）：%s", exc)
        return False
