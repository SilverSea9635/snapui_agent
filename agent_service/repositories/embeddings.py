"""嵌入模型封装（Qwen，1024 维）。

与旧实现保持一致：`init_embeddings(model="openai:<model>", ...)`，
`check_embedding_ctx_length=False`（某些兼容端点在长度探测上会报错）。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from langchain.embeddings import init_embeddings

from ..core.config import settings
from ..core.logging import get_logger

logger = get_logger("embeddings")

_model = None


def get_embeddings():
    """惰性初始化并缓存（单例）。"""
    global _model
    if _model is None:
        if not settings.QWEN_EMBED_API_KEY:
            raise RuntimeError("缺少 QWEN_EMBED_API_KEY，无法初始化嵌入模型")
        _model = init_embeddings(
            model=f"openai:{settings.EMBED_MODEL}",
            api_key=settings.QWEN_EMBED_API_KEY,
            base_url=settings.QWEN_EMBED_BASE_URL,
            check_embedding_ctx_length=False,
        )
        logger.info("嵌入模型就绪：%s（%d 维）", settings.EMBED_MODEL, settings.EMBED_DIM)
    return _model


def embed_query(text: str) -> List[float]:
    return get_embeddings().embed_query(text)


def embed_documents(texts: List[str]) -> List[List[float]]:
    return get_embeddings().embed_documents(texts)


async def aembed_query(text: str) -> List[float]:
    """异步包装：避免同步网络调用阻塞事件循环。"""
    return await asyncio.to_thread(embed_query, text)


async def aembed_documents(texts: List[str]) -> List[List[float]]:
    return await asyncio.to_thread(embed_documents, texts)


def probe(dim: Optional[int] = None) -> dict:
    """探活：真实调用一次嵌入接口，返回维度是否与配置一致。"""
    vector = embed_query("ping")
    return {
        "model": settings.EMBED_MODEL,
        "dim": len(vector),
        "expectedDim": dim or settings.EMBED_DIM,
        "ok": len(vector) == (dim or settings.EMBED_DIM),
    }
