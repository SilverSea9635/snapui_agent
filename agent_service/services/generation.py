"""共享 Agent 执行图的文本流适配与本轮用量统计。"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import aclosing
import uuid
from typing import Any, AsyncIterator, List, Optional

from langchain_core.messages import BaseMessage

from ..agent.llm.cache import get_model_cache
from ..agent.llm.factory import ModelSpec, default_spec, resolve_model_name
from ..agent.runtime import get_agent_runtime, chunk_text as _chunk_text, chunk_usage as _chunk_usage
from ..core.errors import AppError, UpstreamModelError
from ..core.logging import get_logger

logger = get_logger("generation")


@dataclass
class GenerationStats:
    """生成过程中的用量与状态（供 done 事件与计量使用）。"""

    model: str = ""
    usage: Optional[dict[str, Any]] = None
    mode: str = "fresh"


def resolve_mode(has_attachments: bool = False) -> str:
    """P0：附件→adjust（暂未启用）；其余一律 fresh。P1 起读 GENERATION_POLICY。"""
    return "adjust" if has_attachments else "fresh"


async def stream_generation(
    messages: List[BaseMessage],
    stats: GenerationStats,
    model_name: Optional[str] = None,
    spec: Optional[ModelSpec] = None,
    *,
    user_id: str = "",
    conversation_id: Optional[str] = None,
    rag_context: str = "",
    original_content: Any = None,
    message_id: str | None = None,
    assistant_id: str | None = None,
) -> AsyncIterator[str]:
    """流式产出文本增量，并把用量写回 `stats`。

    模型侧的异常统一包成 `UpstreamModelError`：对外只回安全描述，
    上游报错体（可能含密钥/内网地址）只进服务端日志。
    """
    resolved_name = resolve_model_name(model_name) if spec is None else spec.model_name
    stats.model = resolved_name
    model_spec = spec or default_spec()
    if spec is None:
        model_spec.model_name = resolved_name

    try:
        runtime = get_agent_runtime()
        async with aclosing(runtime.stream(
            messages, spec=model_spec, cache=get_model_cache(),
            user_id=user_id, conversation_id=conversation_id or str(uuid.uuid4()),
            rag_context=rag_context,
            original_content=original_content, message_id=message_id, assistant_id=assistant_id,
        )) as stream:
            async for event in stream:
                usage = event.get("usage")
                if usage:
                    previous = stats.usage or {}
                    stats.usage = {key: (previous.get(key) or 0) + value for key, value in usage.items()}
                if event.get("text"):
                    yield event["text"]
    except AppError:
        raise
    except Exception as exc:  # 上游原文和凭据不得进入对外错误。
        logger.error("Model streaming failed (model=%s, type=%s)", resolved_name, type(exc).__name__)
        raise UpstreamModelError() from None
