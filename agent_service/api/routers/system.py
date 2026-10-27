"""系统路由：服务发现 / 健康检查 / 模型列表。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from ...core.config import settings
from ...repositories import embeddings
from ...repositories.vector_store import vector_store
from ...agent.runtime import get_agent_runtime

router = APIRouter(tags=["system"])

#: 探活超时：健康检查必须快速返回，不能被不可达的 Milvus 拖住
PROBE_TIMEOUT = 3.0


async def _probe(coro, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return {**default, "error": f"timeout>{PROBE_TIMEOUT}s"}
    except Exception as exc:
        return {**default, "error": type(exc).__name__}


async def _checkpoint_health() -> dict[str, Any]:
    return await get_agent_runtime().health()


@router.get("/")
async def root():
    """服务发现（键与旧实现保持一致，只新增字段）。"""
    return {
        "name": "snapui-agent-service",
        "version": "1.0.0",
        "appEnv": settings.APP_ENV,
        "chatEndpoint": "POST /api/chat",
        "historyEndpoints": "由 OCRServer 提供（GET/POST/DELETE /api/history）",
        "storage": "postgres-messages-and-memory" if settings.POSTGRES_DSN else "none",
        "agent": {"sharedGraph": True, "webSearch": settings.AGENT_WEB_SEARCH},
        "knowledgeEndpoints": {
            "ingest": "POST /api/knowledge/ingest",
            "search": "POST /api/knowledge/search",
            "stats": "GET /api/knowledge/stats",
        },
        "model": settings.DEEPSEEK_MODEL,
        "availableModels": settings.AVAILABLE_MODELS,
        "modelsEndpoint": "GET /api/models",
        "milvus": settings.MILVUS_URI,
        "generationPolicy": settings.GENERATION_POLICY,
        "internalAuthMode": settings.INTERNAL_AUTH_MODE,
    }


@router.get("/health")
async def health(deep: int = Query(default=0, description="deep=1 时额外真实调用一次嵌入模型")):
    """真实探活：Milvus 连通性；`deep=1` 再验证嵌入模型维度。

    配置 PostgreSQL 时同步探测会话检查点连接。
    """
    milvus_state = await _probe(vector_store.aping(), {"ok": False})
    checkpoint_state = await _probe(_checkpoint_health(), {"ok": False, "backend": "postgres" if settings.POSTGRES_DSN else "none"})

    embedding_state: dict[str, Any] = {"ok": None, "note": "未探测（加 ?deep=1 触发）"}
    if deep:
        embedding_state = await _probe(asyncio.to_thread(embeddings.probe), {"ok": False})

    milvus_ok = bool(milvus_state.get("ok"))
    embedding_ok = embedding_state.get("ok") is not False

    return {
        "status": "ok" if (milvus_ok and embedding_ok and checkpoint_state.get("ok")) else "degraded",
        # 兼容旧字段
        "model": settings.DEEPSEEK_MODEL,
        "milvus": settings.MILVUS_URI,
        # 结构化探活结果
        "checks": {
            "milvus": milvus_state,
            "embedding": embedding_state,
            "checkpoints": checkpoint_state,
        },
        "appEnv": settings.APP_ENV,
        "warnings": settings.warnings,
    }


@router.get("/api/models")
async def list_models():
    """平台预置模型列表（前端下拉框数据源）。"""
    return {"models": settings.AVAILABLE_MODELS, "default": settings.DEEPSEEK_MODEL}
