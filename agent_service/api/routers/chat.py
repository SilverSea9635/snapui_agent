"""对话相关路由：`POST /api/chat`（SSE）。

业务会话历史仍由 OCRServer 提供；运行时检查点通过内部删除接口联动清理。
"""
from __future__ import annotations
from contextlib import aclosing

from fastapi import APIRouter, Request, Query
import hashlib
import hmac
import time
from collections import deque
from uuid import UUID
from ...core.config import settings
from ...core.errors import Unauthorized, AppError, AgentStorageError
from ...core.security import verify_request
from fastapi.responses import StreamingResponse

from ...schemas.models import ChatRequest, MemoryIn
from ...services.chat_service import run_chat, validate_request
from ...agent.runtime import get_agent_runtime, thread_id_for
from ..deps import resolve_user_id
from ..sse import SSE_HEADERS, encode_events

router = APIRouter(prefix="/api", tags=["chat"])

# 自定义模型每个用户最多同时两轮，每分钟最多二十次；记录按空闲时间清理。
_custom_active: dict[str, int] = {}
_custom_times: dict[str, deque[float]] = {}

class ModelRateLimit(AppError):
    code = "model_rate_limit"
    http_status = 429
    safe_message = "模型请求过于频繁，请稍后重试"

def acquire_model_slot(user_id: str):
    now = time.monotonic()
    for key in list(_custom_times):
        if not _custom_active.get(key) and (not _custom_times[key] or now - _custom_times[key][-1] > 60):
            _custom_times.pop(key, None)
    times = _custom_times.setdefault(user_id, deque())
    while times and now - times[0] > 60:
        times.popleft()
    if len(times) >= 20 or _custom_active.get(user_id, 0) >= 2:
        raise ModelRateLimit()
    times.append(now)
    _custom_active[user_id] = _custom_active.get(user_id, 0) + 1
    async def events(req):
        try:
            async with aclosing(run_chat(req, user_id)) as stream:
                async for event in stream:
                    yield event
        finally:
            _custom_active[user_id] -= 1
            if not _custom_active[user_id]:
                _custom_active.pop(user_id)
    return events



@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """接收多模态消息，经 RAG 增强后流式返回生成结果。

    校验在开流之前完成（与旧行为一致：空消息返回 HTTP 400，而不是 SSE error）。
    PostgreSQL 消息与检查点负责历史恢复；上游 history 仅供本地兼容模式。
    """
    if req.modelConfig:
        secret = settings.INTERNAL_API_TOKEN
        verified = verify_request(method="POST", path="/api/chat", headers=request.headers,
                                  mode="enforce", secret=secret, max_skew_seconds=60)
        cfg = req.modelConfig
        timestamp = request.headers.get("x-internal-timestamp", "")
        signed = "\n".join([verified.user_id or "", timestamp, cfg.baseUrl, cfg.modelName, cfg.apiKey])
        expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not verified.verified or not hmac.compare_digest(expected, request.headers.get("x-model-config-signature", "")):
            raise Unauthorized()
    validate_request(req)
    user_id = await resolve_user_id(request, fallback_user_id=req.userId)
    return StreamingResponse(
        encode_events(acquire_model_slot(user_id)(req) if req.modelConfig else run_chat(req, user_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.delete("/agent/sessions/{conversation_id}")
async def delete_agent_session(conversation_id: str, request: Request):
    """由 OCRServer 校验业务归属后调用，删除消息、事件和运行时检查点。"""
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    await get_agent_runtime().delete_session(user_id, conversation_id)
    return {"conversationId": conversation_id, "cleared": True}


@router.get("/agent/sessions/{conversation_id}/messages")
async def list_agent_messages(conversation_id: str, request: Request, limit: int = Query(default=100, ge=1, le=200), before: int | None = Query(default=None, ge=1)):
    """内部历史接口；OCRServer 先校验会话归属，再转发签名请求。"""
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    runtime = get_agent_runtime()
    if runtime.messages is None:
        raise AgentStorageError("消息存储尚未配置，请启用 PostgreSQL")
    return await runtime.messages.list_messages(thread_id_for(user_id, conversation_id), user_id, limit=limit, before=before)


@router.get("/agent/memories")
async def list_agent_memories(request: Request):
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    runtime = get_agent_runtime()
    if runtime.memory is None:
        raise AgentStorageError()
    items = await runtime.memory.asearch(runtime.memory_namespace(user_id), limit=50)
    return {"memories": [{"key": item.key, "value": item.value.get("value", "")} for item in items]}


@router.get("/agent/sessions/{conversation_id}/messages/{message_id}")
async def get_agent_message(conversation_id: str, message_id: UUID, request: Request):
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    runtime = get_agent_runtime()
    if runtime.messages is None:
        raise AgentStorageError()
    return await runtime.messages.get_message(thread_id_for(user_id, conversation_id), user_id, str(message_id))


@router.put("/agent/memories/{key}")
async def put_agent_memory(key: str, req: MemoryIn, request: Request):
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    runtime = get_agent_runtime()
    if runtime.memory is None:
        raise AgentStorageError()
    runtime.validate_memory(key, req.value)
    await runtime.memory.aput(runtime.memory_namespace(user_id), key, {"value": req.value})
    return {"key": key, "saved": True}


@router.delete("/agent/memories/{key}")
async def delete_agent_memory(key: str, request: Request):
    user_id = await resolve_user_id(request, fallback_user_id=request.query_params.get("userId"))
    runtime = get_agent_runtime()
    if runtime.memory is None:
        raise AgentStorageError()
    await runtime.memory.adelete(runtime.memory_namespace(user_id), key)
    return {"key": key, "deleted": True}
