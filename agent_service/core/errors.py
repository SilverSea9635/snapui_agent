"""统一错误类型：对外只暴露「安全描述」，详情只进服务端日志。

背景：旧实现把上游异常原文直接塞进 SSE（`{"error": str(exc)}`），
上游报错体可能夹带密钥或内网地址 → P0 改为「错误码 + 安全描述」。
"""
from __future__ import annotations

from typing import Any, Optional


class AppError(Exception):
    """业务错误基类。`code` 会出现在 SSE / HTTP 响应的 type 字段里。"""

    code = "internal_error"
    http_status = 500
    #: 对外可见的安全描述（不得包含上游原文、密钥、内部地址）
    safe_message = "服务内部错误"

    def __init__(self, safe_message: Optional[str] = None, *, detail: Optional[str] = None):
        self.safe_message = safe_message or self.safe_message
        #: 仅用于服务端日志的补充信息
        self.detail = detail
        super().__init__(self.safe_message)

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.safe_message, "type": self.code}


class BadRequest(AppError):
    code = "bad_request"
    http_status = 400
    safe_message = "请求参数不合法"


class Unauthorized(AppError):
    code = "unauthorized"
    http_status = 401
    safe_message = "未通过内部鉴权"


class Forbidden(AppError):
    code = "forbidden"
    http_status = 403
    safe_message = "无权访问该资源"


class NotFound(AppError):
    code = "not_found"
    http_status = 404
    safe_message = "资源不存在"


class ConfigInvalid(AppError):
    code = "config_invalid"
    http_status = 500
    safe_message = "服务配置不合法，请检查启动日志"


class SessionDomainReadOnly(AppError):
    """会话域（conversations/messages）归 OCRServer，本服务不得写。"""

    code = "session_domain_read_only"
    http_status = 501
    safe_message = "会话的写入与删除由后端 OCRServer 负责，本服务只读"


class UpstreamModelError(AppError):
    """调用生成/嵌入模型失败。绝不回显上游原文。"""

    code = "model_error"
    http_status = 502
    safe_message = "模型端点调用失败"

    def __init__(self, safe_message: Optional[str] = None, *, detail: Optional[str] = None):
        super().__init__(safe_message or "模型端点调用失败，请检查模型配置或稍后重试", detail=detail)


class VectorStoreError(AppError):
    code = "vector_store_error"
    http_status = 503
    safe_message = "向量服务暂不可用"


def to_sse_payload(exc: BaseException) -> dict[str, Any]:
    """把任意异常转成可安全外发的 SSE payload。"""
    if isinstance(exc, AppError):
        return exc.to_payload()
    return {"error": "服务内部错误", "type": "internal_error"}
