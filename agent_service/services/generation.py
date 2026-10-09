"""生成流水线。

P0 只实现 `legacy` 单档（= 旧行为：RAG 增强后一次性生成）；
P1 会在此处接入「直接复用 / 参考生成 / 全量生成」三档策略（`GENERATION_POLICY=reuse`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, List, Optional

from langchain_core.messages import BaseMessage

from ..agent.llm.cache import get_model_cache
from ..agent.llm.factory import ModelSpec, default_spec, resolve_model_name
from ..core.errors import UpstreamModelError
from ..core.logging import get_logger

logger = get_logger("generation")


@dataclass
class GenerationStats:
    """生成过程中的用量与状态（供 done 事件与计量使用）。"""

    model: str = ""
    usage: Optional[dict[str, Any]] = None
    mode: str = "fresh"


def _chunk_text(chunk: Any) -> str:
    """从 AIMessageChunk 中取出文本（兼容 str / 内容块数组）。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts)
    return ""


def _chunk_usage(chunk: Any) -> Optional[dict[str, Any]]:
    """提取用量（langchain-core 的 usage_metadata；部分端点只在末尾给出）。"""
    usage = getattr(chunk, "usage_metadata", None)
    if not usage:
        return None
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def resolve_mode(has_attachments: bool = False) -> str:
    """P0：附件→adjust（暂未启用）；其余一律 fresh。P1 起读 GENERATION_POLICY。"""
    return "adjust" if has_attachments else "fresh"


async def stream_generation(
    messages: List[BaseMessage],
    stats: GenerationStats,
    model_name: Optional[str] = None,
    spec: Optional[ModelSpec] = None,
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

    model = get_model_cache().get(model_spec)

    try:
        async for chunk in model.astream(messages):
            usage = _chunk_usage(chunk)
            if usage:
                stats.usage = usage
            text = _chunk_text(chunk)
            if text:
                yield text
    except Exception as exc:  # 上游细节只进日志（已被日志脱敏过滤器处理）
        logger.exception("模型流式生成失败（model=%s）", resolved_name)
        raise UpstreamModelError(detail=f"{type(exc).__name__}: {exc}") from exc
