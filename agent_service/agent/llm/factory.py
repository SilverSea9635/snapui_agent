"""生成模型工厂。

P0 只支持「平台级默认模型」（`.env` 提供）；用户自带模型（D2/D4）在 P4 接入，
这里已经把 `ModelSpec` 抽象出来，届时只需新增 provider 分支。

顺带修掉的旧问题：
- 默认模型名 `deepseek-v4-flash` 在端点上不存在 → 改为 `deepseek-flash`；
- `init_chat_model(model_provider="deepseek")` 只能读进程级 env，无法承载自定义端点 → 显式传参。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel

from ...core.config import settings
from ...core.errors import BadRequest

SUPPORTED_PROVIDERS = ("deepseek", "openai-compatible")


@dataclass
class ModelSpec:
    """一次生成所需的模型参数（可来自平台默认，也可来自用户配置）。"""

    provider: str
    model_name: str
    api_key: str
    base_url: str
    timeout: float = 60.0
    max_retries: int = 1
    stream_usage: bool = True
    extra_body: dict[str, Any] = field(default_factory=dict)

    def cache_key(self) -> str:
        """缓存键：不含明文密钥（用哈希），换 Key 即换实例，避免不同用户串用。"""
        key_hash = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
        raw = f"{self.provider}|{self.base_url}|{self.model_name}|{key_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def safe_label(self) -> str:
        """日志友好标识（不含密钥）。"""
        return f"{self.provider}:{self.model_name}@{self.base_url}"


def default_spec() -> ModelSpec:
    """平台默认模型配置。"""
    return ModelSpec(
        provider="deepseek",
        model_name=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_API_BASE,
    )


def resolve_model_name(requested: Optional[str]) -> str:
    """把用户传入的模型名规范化：白名单内直接用，否则回落默认（与旧行为一致）。

    注意 P4 起用户可自带任意模型名，届时这条白名单只作用于「平台预置模型」。
    """
    if requested and requested in settings.AVAILABLE_MODELS:
        return requested
    return settings.DEEPSEEK_MODEL


def build_chat_model(spec: ModelSpec) -> BaseChatModel:
    """按 provider 构造 chat 模型实例。"""
    if spec.provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        return ChatDeepSeek(
            model=spec.model_name,
            api_key=spec.api_key,
            api_base=spec.base_url,
            timeout=spec.timeout,
            max_retries=spec.max_retries,
        )

    if spec.provider == "openai-compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=spec.model_name,
            api_key=spec.api_key,
            base_url=spec.base_url,
            timeout=spec.timeout,
            max_retries=spec.max_retries,
            stream_usage=spec.stream_usage,
            **({"extra_body": spec.extra_body} if spec.extra_body else {}),
        )

    raise BadRequest(
        f"暂不支持的模型 provider：{spec.provider}（可选：{', '.join(SUPPORTED_PROVIDERS)}）"
    )
