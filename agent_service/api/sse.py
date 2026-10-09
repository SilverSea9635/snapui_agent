"""SSE 编码。

帧格式与旧实现逐字节一致（`event: X\\ndata: {...}\\n\\n`，UTF-8 不转义），
因为上游 OCRServer 的解析器按 `data: ` 前缀与 `\\n\\n` 分帧（见 DESIGN §5.8）。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ..core.errors import to_sse_payload
from ..core.logging import get_logger

logger = get_logger("sse")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

Event = tuple[str, dict[str, Any]]


def encode(event: str, payload: dict[str, Any]) -> str:
    """格式化为一条 SSE 帧。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def encode_events(events: AsyncIterator[Event]) -> AsyncIterator[str]:
    """把事件流编码为 SSE 帧；异常统一转成 error 事件（绝不外发原文）。"""
    try:
        async for name, payload in events:
            yield encode(name, payload)
    except Exception as exc:  # 流已开始，无法改 HTTP 状态码，只能发 error 事件
        logger.exception("SSE 事件流异常终止")
        yield encode("error", to_sse_payload(exc))
