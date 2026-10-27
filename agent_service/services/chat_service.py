"""一轮对话的编排。

职责边界：会话元数据和组件资产由 OCRServer 管理。
- 本服务以 PostgreSQL 保存原始消息、助手回复、事件、检查点和用户长期偏好。
- PostgreSQL 消息是历史来源；无持久化的本地兼容模式才使用上游 history。
- 本服务持有共享执行图，模型客户端按本轮路由选择。
- RAG 与系统提示词仅参与本轮模型输入。
"""
from __future__ import annotations

import uuid
from contextlib import aclosing
from typing import Any, AsyncIterator

from ..agent.prompts import DEFAULT_SYSTEM_PROMPT
from ..agent.llm.factory import ModelSpec
from ..core.model_http import validate_url
from ..core.config import settings
from ..core.errors import BadRequest, to_sse_payload
from ..core.logging import get_logger
from ..repositories.vector_store import vector_store
from ..schemas.models import ChatRequest
from . import messages as msg_utils
from .generation import GenerationStats, resolve_mode, stream_generation

logger = get_logger("chat")

#: 送入模型的历史条数上限（P2 会换成按 token 预算裁剪）
HISTORY_LIMIT = 20


def _normalize_history(raw: Any) -> list[dict[str, Any]]:
    """把 OCRServer 下发的 history 规整为 build_messages 需要的形状。

    - 只保留 role/content 两个键（多余字段一律忽略，避免上游演进时把噪声带进提示词）；
    - 只取**最近** HISTORY_LIMIT 条（上游可能下发全量）；
    - 非法项直接丢弃而不是报错：历史不完整不应该让整轮对话失败。
    """
    items: list[dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            role, content = entry.get("role"), entry.get("content")
        else:  # pydantic 模型
            role, content = getattr(entry, "role", None), getattr(entry, "content", None)
        if role in ("user", "assistant") and content is not None:
            items.append({"role": role, "content": content})
    if len(items) > HISTORY_LIMIT:
        items = items[-HISTORY_LIMIT:]
    return items

Event = tuple[str, dict[str, Any]]


def _reject_unimplemented(req: ChatRequest) -> None:
    """P0 明确拒绝尚未实现的可选字段，避免「静默忽略」造成的错误结果。"""
    if req.modelProfileId:
        raise BadRequest("配置档由 OCRServer 解析，Python 不读取 MySQL 模型配置")
    if req.modelConfig:
        cfg = req.modelConfig
        validate_url(cfg.baseUrl)
        if cfg.provider != "openai-compatible" or not cfg.modelName.strip() or not cfg.apiKey:
            raise BadRequest("模型配置不完整或 provider 不支持")
        if cfg.extraBody or cfg.timeoutSeconds:
            raise BadRequest("本期暂不支持模型额外参数")
    if req.attachments:
        raise BadRequest(
            "组件附件（把源码引入上下文）尚未启用（计划 P2）。请先移除 attachments 字段。",
            detail="attachments 在 P0 阶段不支持",
        )


def validate_request(req: ChatRequest) -> tuple[str, int]:
    """开流前的同步校验。

    放在 HTTP 阶段（而不是事件流内部）执行，保证「空消息」等错误返回 400 JSON，
    与旧实现行为一致；`run_chat` 也会调一次做兜底。
    返回提取的文本和图片数量，供后续编排复用。
    """
    _reject_unimplemented(req)
    text = msg_utils.extract_text_from_message(req.message)
    image_count = msg_utils.count_images(req.message)
    if not text.strip() and image_count == 0:
        raise BadRequest("message/content 不能为空，必须是字符串或内容块数组")
    return text, image_count


async def run_chat(req: ChatRequest, user_id: str) -> AsyncIterator[Event]:
    """执行一轮对话，产出 SSE 事件元组 `(event_name, payload)`。"""
    text, image_count = validate_request(req)

    conversation_id = (req.conversationId or "").strip() or str(uuid.uuid4())
    message_id = str(req.messageId or uuid.uuid4())
    assistant_id = str(req.assistantMessageId or uuid.uuid4())

    # 1) 兼容本地无持久化调用；启用 PostgreSQL 时由运行时自行恢复历史。
    history = _normalize_history(req.history)

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

    # 3) RAG 仅放入本轮运行上下文，检查点只保存消息，不反复累积检索片段。
    user_content = msg_utils.build_user_content(text, "", image_count)
    chat_messages = msg_utils.build_messages(
        req.system or DEFAULT_SYSTEM_PROMPT, history, user_content
    )

    stats = GenerationStats(mode=resolve_mode(has_attachments=False))

    yield "start", {"conversationId": conversation_id, "messageId": message_id, "assistantMessageId": assistant_id}

    collected: list[str] = []
    try:
        spec = None
        if req.modelConfig:
            cfg = req.modelConfig
            spec = ModelSpec(provider=cfg.provider, model_name=cfg.modelName,
                             base_url=cfg.baseUrl, api_key=cfg.apiKey)
        async with aclosing(stream_generation(
            chat_messages, stats, model_name=req.model, spec=spec,
            user_id=user_id, conversation_id=conversation_id, rag_context=context,
            original_content=req.originalMessage if req.originalMessage is not None else text,
            message_id=message_id, assistant_id=assistant_id,
        )) as stream:
            async for chunk in stream:
                collected.append(chunk)
                yield "delta", {"text": chunk}
    except Exception as exc:
        logger.error("对话生成失败（conversation=%s, type=%s）", conversation_id, type(exc).__name__)
        yield "error", to_sse_payload(exc)
        return

    full_text = "".join(collected)

    # 4) 回合元数据（mode/usage 等）随 done 帧交给 OCRServer 落库 —— 本服务不写库
    yield "done", {
        "conversationId": conversation_id,
        "messageId": message_id,
        "assistantMessageId": assistant_id,
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
