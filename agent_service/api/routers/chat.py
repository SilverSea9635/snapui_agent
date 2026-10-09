"""对话相关路由：`POST /api/chat`（SSE）与清空会话。"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ...core.errors import SessionDomainReadOnly
from ...schemas.models import ChatRequest
from ...services.chat_service import run_chat, validate_request
from ..deps import resolve_user_id
from ..sse import SSE_HEADERS, encode_events

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """接收多模态消息，经 RAG 增强后流式返回生成结果。

    校验在开流之前完成（与旧行为一致：空消息返回 HTTP 400，而不是 SSE error）。
    """
    validate_request(req)
    user_id = await resolve_user_id(request, fallback_user_id=req.userId)
    return StreamingResponse(
        encode_events(run_chat(req, user_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.delete("/chat/{conversation_id}")
async def clear_chat(
    conversation_id: str, request: Request, userId: str = Query(default="")
):
    """清空会话。

    C2 之后会话域归 OCRServer（本服务只读），因此这里明确返回 501，
    由 OCRServer 的 `DELETE /api/chat/:conversationId` 承担实际删除。
    """
    await resolve_user_id(request, fallback_user_id=userId)
    raise SessionDomainReadOnly()
