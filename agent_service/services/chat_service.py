"""一轮对话的编排。

职责边界（C2）：
- **只读**会话域（conversations/messages）；用户消息与助手消息由 OCRServer 落库；
- 本服务只写自有域（回合元数据；P1 起还有组件资产与计量）。
"""
from __future__ import annotations

import uuid
from typing import Any, AsyncIterator, Optional

from ..agent.prompts import DEFAULT_SYSTEM_PROMPT
from ..core.config import settings
from ..core.errors import BadRequest, to_sse_payload
from ..core.logging import get_logger
from ..repositories.sessions import session_repo
from ..repositories.turns import record_turn
from ..repositories.vector_store import vector_store
from ..schemas.models import ChatRequest
from . import messages as msg_utils
from .generation import GenerationStats, resolve_mode, stream_generation

logger = get_logger("chat")

#: 送入模型的历史条数上限（P2 会换成按 token 预算裁剪）
HISTORY_LIMIT = 20

Event = tuple[str, dict[str, Any]]


def _reject_unimplemented(req: ChatRequest) -> None:
    """P0 明确拒绝尚未实现的可选字段，避免「静默忽略」造成的错误结果。"""
    if req.modelConfig or req.modelProfileId:
        raise BadRequest(
            "用户自带模型尚未启用（计划 P4）。请先移除 modelConfig / modelProfileId 字段。",
            detail="modelConfig/modelProfileId 在 P0 阶段不支持",
        )
    if req.attachments:
        raise BadRequest(
            "组件附件（把源码引入上下文）尚未启用（计划 P2）。请先移除 attachments 字段。",
            detail="attachments 在 P0 阶段不支持",
        )


def validate_request(req: ChatRequest) -> None:
    """开流前的同步校验。

    放在 HTTP 阶段（而不是事件流内部）执行，保证「空消息」等错误返回 400 JSON，
    与旧实现行为一致；`run_chat` 也会调一次做兜底。
    """
    _reject_unimplemented(req)
    text = msg_utils.extract_text_from_message(req.message)
    if not text.strip() and msg_utils.count_images(req.message) == 0:
        raise BadRequest("message/content 不能为空，必须是字符串或内容块数组")


async def run_chat(req: ChatRequest, user_id: str) -> AsyncIterator[Event]:
    """执行一轮对话，产出 SSE 事件元组 `(event_name, payload)`。"""
    validate_request(req)

    text = msg_utils.extract_text_from_message(req.message)
    image_count = msg_utils.count_images(req.message)

    conversation_id = (req.conversationId or "").strip() or str(uuid.uuid4())

    # 1) 历史（只读；会话不存在时为空，由 OCRServer 负责创建会话与写消息）
    history = await session_repo.load_history(conversation_id, user_id, limit=HISTORY_LIMIT)

    # 2) RAG 检索：失败不阻塞生成（向量库不可用时降级）
    context = ""
    degraded = False
    if text.strip():
        try:
            hits = await vector_store.asearch(text, settings.RAG_TOP_K)
            context = msg_utils.format_context(hits)
        except Exception as exc:
            degraded = True
            logger.warning("RAG 检索失败，跳过上下文增强：%s", exc)

    # 3) 组装消息
    user_content = msg_utils.build_user_content(text, context, image_count)
    chat_messages = msg_utils.build_messages(
        req.system or DEFAULT_SYSTEM_PROMPT, history, user_content
    )

    stats = GenerationStats(mode=resolve_mode(has_attachments=False))

    yield "start", {"conversationId": conversation_id}

    collected: list[str] = []
    try:
        async for chunk in stream_generation(chat_messages, stats, model_name=req.model):
            collected.append(chunk)
            yield "delta", {"text": chunk}
    except Exception as exc:
        logger.exception("对话生成失败（conversation=%s）", conversation_id)
        yield "error", to_sse_payload(exc)
        return

    full_text = "".join(collected)

    # 4) 回合元数据（自有域；失败不影响对话）
    await record_turn(
        conversation_id,
        user_id,
        generation_mode=stats.mode,
        model=stats.model,
        attachments=[],
        tool_trace=[],
    )

    yield "done", {
        "conversationId": conversation_id,
        "stopReason": "stop",
        "model": stats.model,
        "usage": stats.usage,
        # 新增可选字段（OCRServer 升级后会白名单透传给前端）
        "mode": stats.mode,
        "reused": False,
        "componentId": None,
        "similarity": None,
        "degraded": degraded,
        "characters": len(full_text),
    }


async def load_history_view(conversation_id: str, user_id: str) -> Optional[dict]:
    """历史详情（只读）。"""
    return await session_repo.load_conversation(conversation_id, user_id)
