"""FastAPI 应用装配（重写后的入口）。

启动顺序（很重要）：
1. `create_app()` 里先做**配置校验** —— 配置非法直接抛错，进程绝不进入监听状态；
2. lifespan 打开 MySQL 双池并执行幂等迁移；
3. 迁移失败不阻塞启动（服务可降级为「只读不可用」），但会记 error 日志并在 /health 暴露。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.routers import chat, history, knowledge, system
from .core.config import ConfigError, settings
from .core.errors import AppError
from .core.http import configure_outbound_proxy
from .core.logging import get_logger, install_redaction
from .repositories.mysql import mysql

logger = get_logger("main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("启动配置：%s", settings.to_safe_dict())
    for warning in settings.warnings:
        logger.warning("配置告警：%s", warning)

    await mysql.open()
    try:
        applied = await mysql.migrate()
        logger.info("自有域迁移完成：%s", applied or "无变化")
    except Exception as exc:
        # 迁移失败不阻止启动：健康检查会暴露问题，且读路径可能仍可用
        logger.error("数据库迁移失败（服务继续启动，/health 会显示异常）：%s", exc)

    yield

    await mysql.close()
    logger.info("已释放 MySQL 连接池")


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
        description="FastAPI agent 服务：DeepSeek 生成 + Milvus 检索 + 组件资产域（MySQL）",
    )

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
    app.include_router(history.router)
    app.include_router(knowledge.router)
    return app


app = create_app()
