"""路由依赖：解析并（按模式）校验调用方身份。

身份来源优先级：
1. `X-User-Id` + `X-Internal-Signature`（OCRServer 的签名调用，推荐）；
2. 请求体/查询参数里的 `userId`（兼容旧调用方式；enforce 模式下**不被信任**）。

模式（`INTERNAL_AUTH_MODE`）：
- `off`     ：不校验，直接使用 userId（本地开发默认，等价旧行为）
- `warn`    ：校验失败只告警、仍放行（本服务先上线、OCRServer 后加签名的宽限期）
- `enforce` ：校验失败 401
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from ..core.config import settings
from ..core.errors import Unauthorized
from ..core.logging import get_logger
from ..core.security import require_user_id, verify_request

logger = get_logger("auth")


async def resolve_user_id(request: Request, fallback_user_id: Optional[str] = None) -> str:
    """解析当前调用方的用户标识（`users.id` 的十进制字符串）。"""
    result = verify_request(
        method=request.method,
        path=request.url.path,
        headers=request.headers,
        mode=settings.INTERNAL_AUTH_MODE,
        secret=settings.INTERNAL_API_TOKEN,
        max_skew_seconds=settings.INTERNAL_AUTH_MAX_SKEW,
        fallback_user_id=fallback_user_id,
    )

    if not result.verified and settings.INTERNAL_AUTH_MODE == "enforce":
        logger.warning(
            "内部鉴权失败（enforce 拒绝）：reason=%s path=%s", result.reason, request.url.path
        )
        raise Unauthorized(detail=f"reason={result.reason}")

    if not result.verified and settings.INTERNAL_AUTH_MODE == "warn":
        logger.warning(
            "内部鉴权未通过（warn 模式放行）：reason=%s path=%s 用户=%s",
            result.reason,
            request.url.path,
            result.user_id,
        )

    return require_user_id(result)


async def require_internal_auth(request: Request) -> None:
    """只校验「服务间签名」，不要求用户身份。

    用于知识库这类管理/运维接口：旧实现无鉴权也接受无 userId 的调用，
    这里保持该用法可用（`INTERNAL_AUTH_MODE=off`），但在 enforce 模式下仍要求签名调用。
    """
    result = verify_request(
        method=request.method,
        path=request.url.path,
        headers=request.headers,
        mode=settings.INTERNAL_AUTH_MODE,
        secret=settings.INTERNAL_API_TOKEN,
        max_skew_seconds=settings.INTERNAL_AUTH_MAX_SKEW,
        fallback_user_id=None,
    )
    if not result.verified and settings.INTERNAL_AUTH_MODE == "enforce":
        logger.warning("内部鉴权失败（enforce 拒绝）：reason=%s path=%s", result.reason, request.url.path)
        raise Unauthorized(detail=f"reason={result.reason}")
    if not result.verified and settings.INTERNAL_AUTH_MODE == "warn":
        logger.warning("内部鉴权未通过（warn 模式放行）：reason=%s path=%s", result.reason, request.url.path)
