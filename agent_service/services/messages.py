"""消息与上下文的转换（纯函数，便于单测）。

来源：旧 `agent.py` 里的同名能力，行为保持一致（兼容 Pydantic 对象与 dict 两种块形态）。
"""
from __future__ import annotations

from typing import Any, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from ..agent.prompts import CONTEXT_TEMPLATE, IMAGE_HINT


def _block_type(block: Any) -> Optional[str]:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("text") or ""
    return getattr(block, "text", None) or ""


def extract_text_from_message(message: Any) -> str:
    """从字符串或内容块数组里取纯文本。"""
    if isinstance(message, str):
        return message
    parts: List[str] = []
    for block in message or []:
        if _block_type(block) == "text":
            text = _block_text(block)
            if text:
                parts.append(text)
    return "\n".join(parts)


def count_images(message: Any) -> int:
    """统计图片块数量（像素本身不进入本服务，仅用于提示词）。"""
    if isinstance(message, str):
        return 0
    return sum(1 for block in (message or []) if _block_type(block) == "image")


def content_to_text(content: Any) -> str:
    """把历史消息的 content（str / list / dict）还原为文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", "") or ""
    return str(content or "")


def format_context(hits: List[dict]) -> str:
    """把检索结果格式化为带编号与元数据的上下文块。"""
    if not hits:
        return ""
    blocks = []
    for index, hit in enumerate(hits, 1):
        blocks.append(
            f"[片段{index} | source={hit.get('source', '')} | score={hit.get('score', 0.0):.4f}]\n"
            f"{hit.get('text', '')}"
        )
    return "\n\n".join(blocks)


def build_user_content(text: str, context: str = "", image_count: int = 0) -> str:
    """构造最终发给模型的用户消息（RAG 上下文 + 图片提示 + 用户需求）。"""
    parts: List[str] = []
    if context:
        parts.append(CONTEXT_TEMPLATE.format(context=context))
    if image_count > 0:
        parts.append(IMAGE_HINT)
    if text.strip():
        parts.append(f"<用户问题>\n{text}\n</用户问题>")
    return "\n\n".join(parts)


def build_messages(
    system: str, history: List[dict], user_content: str
) -> List[BaseMessage]:
    """组装 system + 历史 + 当前用户消息。"""
    messages: List[BaseMessage] = []
    if system:
        messages.append(SystemMessage(content=system))

    for item in history:
        role = item.get("role")
        content = content_to_text(item.get("content", ""))
        if not content:
            continue
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=user_content))
    return messages
