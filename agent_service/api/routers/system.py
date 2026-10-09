"""系统路由：服务发现 / 健康检查 / 模型列表。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from ...core.config import settings
from ...repositories import embeddings
from ...repositories.mysql import mysql
from ...repositories.vector_store import vector_store

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


@router.get("/")
async def root():
    """服务发现（键与旧实现保持一致，只新增字段）。"""
    return {
        "name": "snapui-agent-service",
        "version": "1.0.0",
        "appEnv": settings.APP_ENV,
        "chatEndpoint": "POST /api/chat",
        "historyEndpoints": {
            "list": "GET /api/history",
            "detail": "GET /api/history/{conversationId}",
            "create": "POST /api/history  （501：由 OCRServer 负责）",
            "delete": "DELETE /api/history/{conversationId}  （501：由 OCRServer 负责）",
        },
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
    """真实探活：MySQL 两个池、Milvus 连通性；`deep=1` 再验证嵌入模型维度。"""
    mysql_state = await _probe(mysql.ping(), {"rw": "unknown", "ro": "unknown"})
    milvus_state = await _probe(vector_store.aping(), {"ok": False})

    embedding_state: dict[str, Any] = {"ok": None, "note": "未探测（加 ?deep=1 触发）"}
    if deep:
        embedding_state = await _probe(asyncio.to_thread(embeddings.probe), {"ok": False})

    mysql_ok = mysql_state.get("rw") == "ok" and mysql_state.get("ro") == "ok"
    milvus_ok = bool(milvus_state.get("ok"))
    embedding_ok = embedding_state.get("ok") is not False

    return {
        "status": "ok" if (mysql_ok and milvus_ok and embedding_ok) else "degraded",
        # 兼容旧字段
        "model": settings.DEEPSEEK_MODEL,
        "postgres": "removed（C2：存储已收敛到 MySQL）",
        "milvus": settings.MILVUS_URI,
        # 结构化探活结果
        "checks": {
            "mysql": mysql_state,
            "milvus": milvus_state,
            "embedding": embedding_state,
        },
        "appEnv": settings.APP_ENV,
        "warnings": settings.warnings,
    }


@router.get("/api/models")
async def list_models():
    """平台预置模型列表（前端下拉框数据源）。"""
    return {"models": settings.AVAILABLE_MODELS, "default": settings.DEEPSEEK_MODEL}
