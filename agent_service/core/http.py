"""出站网络策略：是否信任环境里的代理变量。

背景（本机实测踩到的坑）：环境里存在 `all_proxy=socks5://127.0.0.1:7890`，而该端口
并没有监听。`httpx`（OpenAI/LangChain 的底层）会读取它，并在**构造客户端时**直接抛
`ImportError: Using SOCKS proxy, but the 'socksio' package is not installed` ——
于是任何模型调用都会失败，且错误信息完全指不到真正原因。

决策：**默认不信任环境代理**（`HTTP_TRUST_ENV=false`）。需要走代理的部署显式声明：
- `HTTP_TRUST_ENV=true` 表示沿用环境变量；
- `MODEL_HTTP_PROXY=http://host:port` 表示只给本服务的出站请求设置该代理。

这样做同时消除了「生产环境里某个无关变量悄悄改变出站行为」的不确定性。
"""
from __future__ import annotations

import os

from .config import settings
from .logging import get_logger

logger = get_logger("http")

PROXY_ENV_KEYS = (
    "all_proxy",
    "ALL_PROXY",
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
)
#: no_proxy 保留不动：它是「排除列表」，清掉反而是错的


def configure_outbound_proxy() -> list[str]:
    """按配置处理代理环境变量；返回被移除的变量名（不含值，避免泄露凭据）。"""
    if settings.HTTP_TRUST_ENV:
        if settings.MODEL_HTTP_PROXY:
            for key in ("http_proxy", "https_proxy", "all_proxy"):
                os.environ[key] = settings.MODEL_HTTP_PROXY
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                os.environ.pop(key, None)
            logger.info("出站代理：使用 MODEL_HTTP_PROXY 指定的代理")
        else:
            logger.info("出站代理：信任环境变量（HTTP_TRUST_ENV=true）")
        return []

    removed = [key for key in PROXY_ENV_KEYS if os.environ.pop(key, None) is not None]
    if removed:
        logger.info(
            "出站代理：已忽略环境中的代理变量（%s）；需要时设 HTTP_TRUST_ENV=true "
            "或 MODEL_HTTP_PROXY",
            ", ".join(removed),
        )
    return removed
