"""FastAPI 应用装配（重写后的入口）。

启动顺序（很重要）：
1. `create_app()` 里先做**配置校验** —— 配置非法直接抛错，进程绝不进入监听状态；
2. lifespan 记录配置并探测 Milvus 连通性，失败时告警并继续启动；
3. 初始化共享执行图及 PostgreSQL 消息、检查点和长期偏好；生产强制持久化。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .api.routers import chat, knowledge, system
from .core.config import ConfigError, settings
from .core.errors import AppError
from .core.http import configure_outbound_proxy
from .core.logging import get_logger, install_redaction
from .repositories.vector_store import vector_store
from .agent.runtime import agent_lifespan

logger = get_logger("main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("启动配置：%s", settings.to_safe_dict())
    for warning in settings.warnings:
        logger.warning("配置告警：%s", warning)

    started = time.monotonic()
    logger.info("Milvus 启动检查开始：mode=%s db=%s", settings.milvus_mode_resolved, settings.MILVUS_DB or "默认库")
    try:
        state = await asyncio.wait_for(vector_store.aping(), timeout=3.0)
    except Exception as exc:
        logger.warning(
            "Milvus 启动检查失败：type=%s elapsed=%.2fs；服务继续启动，请检查 Milvus 配置与可用性",
            type(exc).__name__, time.monotonic() - started,
        )
    else:
        logger.info(
            "Milvus 启动检查通过：collections=%s elapsed=%.2fs（集合按需初始化）",
            state["collections"], time.monotonic() - started,
        )

    async with agent_lifespan() as runtime:
        _app.state.agent_runtime = runtime
        try:
            yield
        finally:
            del _app.state.agent_runtime

    logger.info("服务已退出")


def create_app() -> FastAPI:
    install_redaction(settings.LOG_LEVEL)
    # 必须早于任何 HTTP 客户端构造（否则 httpx 会在构造期就读取失效的代理变量）
    configure_outbound_proxy()
    try:
        settings.validate()
    except ConfigError as exc:
        # 启动期直接失败，避免带着错误配置对外服务
        logger.error("配置校验失败：%s", exc)
        raise

    app = FastAPI(
        title="SnapUI Agent Service",
        version="1.0.0",
        lifespan=lifespan,
        description="FastAPI Agent 服务：共享 LangGraph、动态模型路由、搜索、PostgreSQL 消息与长期记忆",
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, _exc: RequestValidationError):
        # Pydantic 默认错误含 input；自定义模型请求可能包含密钥，禁止回显。
        return JSONResponse(status_code=422, content={"error": "请求参数格式不正确", "type": "validation_error"})

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError):
        """业务错误统一转换为 HTTP JSON（只在开流前生效；流内错误走 SSE error 事件）。"""
        if exc.detail:
            logger.warning("业务错误 code=%s detail=%s", exc.code, exc.detail)
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, exc: Exception):
        """兜底：未预期异常也只回安全描述，堆栈进服务端日志（已脱敏）。"""
        logger.exception("未处理异常：%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": "服务内部错误", "type": "internal_error"},
        )

    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(knowledge.router)
    return app


app = create_app()
