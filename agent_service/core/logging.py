"""日志与敏感信息脱敏。

要点：**密钥永不进日志**。这里用「格式化后再脱敏」的方式，而不是只脱敏 msg：
`RedactingFormatter.format()` 覆盖整条输出（含异常堆栈），因此 `logger.exception()`
里夹带的请求体、上游响应体、DSN、Token 都会被替换掉。

`redact()` 同时供 SSE 错误响应复用（对外只回安全描述）。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

# 逐条说明：
# 1) sk- 开头的 API Key
# 2) Authorization: Bearer xxx
# 3) key=value / key: value 形式的常见密钥字段
# 4) DSN 里的密码：scheme://user:pass@host
# 5) 80 位以上的十六进制串（Milvus/Zilliz Token 是 96 位 hex；组件 digest 是 64 位 sha256，故不误伤）
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|token|password|passwd|secret|authorization)\b"
            r"(\s*[=:]\s*)([^\s,;&'\"}\]]+)"
        ),
        r"\1\2***",
    ),
    (re.compile(r"://([^:/@\s]+):([^@/\s]+)@"), r"://\1:***@"),
    (re.compile(r"\b[0-9a-fA-F]{80,}\b"), "***"),
]


def redact(text: str) -> str:
    """把文本里的疑似密钥替换为 ***。"""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """先按父类格式化（含 traceback），再整体脱敏。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


def _wrap_handlers(logger: logging.Logger) -> int:
    wrapped = 0
    for handler in list(logger.handlers):
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(
                RedactingFormatter(
                    fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            wrapped += 1
    return wrapped


def install_redaction(level: str = "INFO", extra_loggers: Iterable[str] = ()) -> logging.Logger:
    """安装脱敏：给根 logger 与指定 logger（uvicorn 等）挂上 RedactingFormatter。

    返回本服务的 logger。**必须在 uvicorn 启动前调用**（否则 uvicorn 会覆盖 handler）。
    """
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))
    _wrap_handlers(logging.getLogger())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", *extra_loggers):
        _wrap_handlers(logging.getLogger(name))

    logger = logging.getLogger("snapui")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"snapui.{name}")
