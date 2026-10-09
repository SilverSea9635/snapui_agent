"""安全工具：内部鉴权（OCRServer → 本服务）与密钥掩码。

为什么需要：旧实现里 `userId` 完全来自请求体/查询参数，任何人可伪造他人身份
读写会话与（P4 之后的）用户模型配置。P0 引入共享密钥签名：

    X-User-Id:            <users.id 的十进制字符串>
    X-Internal-Timestamp: <unix 秒>
    X-Internal-Signature: hex(HMAC_SHA256(secret, f"{user_id}.{ts}.{method}.{path}"))

三种模式（`INTERNAL_AUTH_MODE`）：
- `off`     ：不校验（本地开发默认；行为与旧版一致）
- `warn`    ：校验失败只告警、仍放行 —— 用于「本服务先上线、OCRServer 后加签名」的宽限期
- `enforce` ：校验失败返回 401
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from .errors import Unauthorized

USER_ID_HEADER = "x-user-id"
TIMESTAMP_HEADER = "x-internal-timestamp"
SIGNATURE_HEADER = "x-internal-signature"


def build_signature(
    secret: str, user_id: str, timestamp: str, method: str, path: str
) -> str:
    """生成签名（OCRServer 侧需用同一算法）。"""
    message = f"{user_id}.{timestamp}.{method.upper()}.{path}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """掩码显示：sk-****abcd。用于模型配置列表等对外响应。"""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * 4}{value[-keep:]}"


def normalize_user_id(raw: Optional[str]) -> Optional[str]:
    """用户标识统一为 `users.id` 的十进制字符串；非法值返回 None。"""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or not value.isdigit():
        return None
    return value


@dataclass
class InternalAuthResult:
    user_id: Optional[str]
    verified: bool
    reason: Optional[str] = None


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def verify_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    mode: str,
    secret: str,
    max_skew_seconds: int,
    fallback_user_id: Optional[str] = None,
) -> InternalAuthResult:
    """按模式校验内部签名。

    返回结果里带 `verified`：调用方据此决定「warn 模式下降级信任 fallback」的行为，
    并把失败原因写进日志。
    """
    user_id = normalize_user_id(_header(headers, USER_ID_HEADER)) or normalize_user_id(
        fallback_user_id
    )

    if mode == "off":
        return InternalAuthResult(user_id=user_id, verified=False, reason="auth_mode_off")

    signature = _header(headers, SIGNATURE_HEADER)
    timestamp = _header(headers, TIMESTAMP_HEADER)
    signed_user_id = normalize_user_id(_header(headers, USER_ID_HEADER))

    if not secret:
        return InternalAuthResult(user_id=user_id, verified=False, reason="secret_missing")
    if not signature or not timestamp or not signed_user_id:
        return InternalAuthResult(user_id=user_id, verified=False, reason="headers_missing")

    try:
        skew = abs(time.time() - int(timestamp))
    except ValueError:
        return InternalAuthResult(user_id=user_id, verified=False, reason="timestamp_invalid")
    if skew > max_skew_seconds:
        return InternalAuthResult(user_id=user_id, verified=False, reason="timestamp_expired")

    expected = build_signature(secret, signed_user_id, timestamp, method, path)
    if not hmac.compare_digest(expected, signature):
        return InternalAuthResult(user_id=user_id, verified=False, reason="signature_mismatch")

    return InternalAuthResult(user_id=signed_user_id, verified=True)


def require_user_id(result: InternalAuthResult) -> str:
    """取出用户标识；缺失即 401（enforce 语境）。"""
    if not result.user_id:
        raise Unauthorized("缺少合法的用户标识（X-User-Id 或 userId）")
    return result.user_id
