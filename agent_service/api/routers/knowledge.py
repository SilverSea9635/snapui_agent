"""知识库（文档 RAG）路由：写入 / 检索 / 统计。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.config import settings
from ...core.errors import BadRequest
from ...repositories.vector_store import vector_store
from ...schemas.models import IngestRequest, SearchRequest
from ..deps import require_internal_auth

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    """把文本 / 文档写入 Milvus 知识库。"""
    await require_internal_auth(request)

    texts: list[str] = []
    sources: list[str] = []
    if req.texts:
        texts.extend(req.texts)
        sources.extend([""] * len(req.texts))
    for doc in req.documents or []:
        if isinstance(doc, dict) and doc.get("text"):
            texts.append(doc["text"])
            sources.append(doc.get("source", ""))

    if not texts:
        raise BadRequest("texts 或 documents 至少需要一条内容")

    count = await vector_store.aupsert_texts(texts, sources, collection=req.collection)
    return {"ingested": count, "collection": req.collection or settings.MILVUS_COLLECTION}


@router.post("/search")
async def search(req: SearchRequest, request: Request):
    await require_internal_auth(request)
    hits = await vector_store.asearch(req.query, req.top_k, collection=req.collection)
    return {"query": req.query, "hits": hits}


@router.get("/stats")
async def knowledge_stats(request: Request, collection: str | None = None):
    await require_internal_auth(request)
    return await vector_store.astats(collection=collection)
