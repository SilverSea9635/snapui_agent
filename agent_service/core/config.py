"""全局配置（分环境加载 + 启动期校验）。

约定：
- `APP_ENV=local|production`；优先加载 `.env.<APP_ENV>`，不存在则回退 `.env`（保证现有单文件部署仍可用）。
- 所有密钥只从环境变量/`.env` 读取，代码内不出现任何真值。
- 启动即校验（`Settings.validate()`）：配置错误直接让进程起不来，而不是等到第一个请求才炸。
"""
from __future__ import annotations

import os
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv

# 工程根目录 = agent_service/core/config.py 的上两级
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """配置非法：启动期抛出，附带修复建议。"""


def _load_env_file(app_env: str) -> str:
    """分层加载：`.env` 作基座，`.env.<APP_ENV>` 叠加覆盖。

    这样本地只需在 `.env.local` 里写「与基座不同的少数几项」（例如把 Milvus 指向
    standalone），**不必把密钥复制一份**；部署时同理用 `.env.production` 覆盖。
    返回实际加载的文件列表（逗号分隔）。
    """
    # 先快照「进程原有环境变量」：文件里的值不应覆盖显式传入的环境变量
    # （否则 AGENT_PORT=8001 uv run python run.py 这类覆盖会失效，蓝绿发布也做不了）
    process_env = dict(os.environ)

    loaded: list[str] = []
    for name in (".env", f".env.{app_env}"):
        path = PROJECT_ROOT / name
        if path.exists():
            # override=True 让「后加载的文件」覆盖「先加载的文件」，从而支持 .env.local 叠加
            load_dotenv(dotenv_path=path, override=True)
            loaded.append(name)

    # 再把进程环境写回 → 最终优先级：进程环境 > .env.<APP_ENV> > .env
    for key, value in process_env.items():
        os.environ[key] = value

    return ",".join(loaded)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前为 {raw!r}") from exc


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_local_host(host: Optional[str]) -> bool:
    """判断主机是否为「本机/私网」——用于阻止本地开发误连生产 Milvus。"""
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return True
    try:
        ip = ip_address(host)
    except ValueError:
        return False  # 域名一律视为远程
    return ip.is_private or ip.is_loopback or ip.is_link_local


class Settings:
    """集中管理服务配置。字段名与 .env 变量名一一对应。"""

    def __init__(self) -> None:
        # ── 运行环境 ──
        self.APP_ENV: str = _env("APP_ENV", "local") or "local"
        self.ENV_FILE: str = _load_env_file(self.APP_ENV)

        # ── 服务监听 ──
        self.HOST: str = _env("AGENT_HOST", "127.0.0.1")
        self.PORT: int = _env_int("AGENT_PORT", 8000)
        self.LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper()

        # ── Agent 检查点；业务消息迁移前继续由 OCRServer 管理 ──
        self.POSTGRES_DSN: str = _env("POSTGRES_DSN")
        self.POSTGRES_SCHEMA: str = _env("POSTGRES_SCHEMA", "agent_runtime")
        self.POSTGRES_POOL_MAX_SIZE: int = _env_int("POSTGRES_POOL_MAX_SIZE", 5)
        self.POSTGRES_CONNECT_TIMEOUT: int = _env_int("POSTGRES_CONNECT_TIMEOUT", 10)
        self.POSTGRES_AUTO_SETUP: bool = _env_bool("POSTGRES_AUTO_SETUP", True)
        self.POSTGRES_PRUNE_CHECKPOINTS: bool = _env_bool("POSTGRES_PRUNE_CHECKPOINTS", True)
        self.AGENT_MAX_MODEL_CALLS: int = _env_int("AGENT_MAX_MODEL_CALLS", 6)
        self.AGENT_HISTORY_MESSAGES: int = _env_int("AGENT_HISTORY_MESSAGES", 40)
        self.TAVILY_API_KEY: str = _env("TAVILY_API_KEY")
        self.AGENT_WEB_SEARCH: bool = _env_bool("AGENT_WEB_SEARCH", bool(self.TAVILY_API_KEY))

        # ── Milvus（本地 standalone / 生产 Zilliz Cloud）──
        self.MILVUS_URI: str = _env("MILVUS_URI", "http://localhost:19530")
        self.MILVUS_TOKEN: str = _env("MILVUS_TOKEN")
        self.MILVUS_USER: str = _env("MILVUS_USER")
        self.MILVUS_PASSWORD: str = _env("MILVUS_PASSWORD")
        self.MILVUS_DB: str = _env("MILVUS_DB", "snapui_rag")
        self.MILVUS_COLLECTION: str = _env("MILVUS_COLLECTION", "design_knowledge")
        self.MILVUS_COMPONENT_COLLECTION: str = _env(
            "MILVUS_COMPONENT_COLLECTION", "ui_components"
        )
        self.MILVUS_MODE: str = _env("MILVUS_MODE", "auto").lower()  # auto|standalone|cloud
        self.MILVUS_TIMEOUT: float = float(_env("MILVUS_TIMEOUT", "10") or 10)
        self.MILVUS_MAX_RETRIES: int = _env_int("MILVUS_MAX_RETRIES", 2)
        self.MILVUS_ALLOW_REMOTE_IN_LOCAL: bool = _env_bool(
            "MILVUS_ALLOW_REMOTE_IN_LOCAL", False
        )

        # ── 生成模型（平台级默认；用户自带模型见 P4）──
        self.DEEPSEEK_API_KEY: str = _env("DEEPSEEK_API_KEY")
        self.DEEPSEEK_API_BASE: str = _env("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
        # 端点真实模型 id（原默认值 deepseek-v4-flash 不存在，已修正）
        self.DEEPSEEK_MODEL: str = _env("DEEPSEEK_MODEL", "deepseek-flash")
        self.AVAILABLE_MODELS: list[str] = _split_csv(
            _env("AVAILABLE_MODELS", "deepseek-flash,deepseek-v4-pro")
        )

        # ── 嵌入模型（Qwen，1024 维）──
        self.QWEN_EMBED_API_KEY: str = _env("QWEN_EMBED_API_KEY")
        self.QWEN_EMBED_BASE_URL: str = _env("QWEN_EMBED_BASE_URL")
        self.EMBED_MODEL: str = _env("EMBED_MODEL", "qwen3.7-text-embedding")
        self.EMBED_DIM: int = _env_int("EMBED_DIM", 1024)

        # ── RAG / 生成策略 ──
        self.RAG_TOP_K: int = _env_int("RAG_TOP_K", 4)
        self.GENERATION_POLICY: str = _env("GENERATION_POLICY", "legacy").lower()

        # ── 出站网络（模型/嵌入调用）──
        # 默认不信任环境代理：环境里一个失效的 all_proxy 会让 httpx 在构造客户端时就抛
        # ImportError(SOCKS)，且错误现象与真因完全无关（见 core/http.py 的说明）。
        self.HTTP_TRUST_ENV: bool = _env_bool("HTTP_TRUST_ENV", False)
        self.MODEL_HTTP_PROXY: str = _env("MODEL_HTTP_PROXY")

        # ── 内部鉴权（OCRServer → 本服务）──
        self.INTERNAL_AUTH_MODE: str = _env("INTERNAL_AUTH_MODE", "off").lower()
        self.INTERNAL_API_TOKEN: str = _env("INTERNAL_API_TOKEN")
        self.INTERNAL_AUTH_MAX_SKEW: int = _env_int("INTERNAL_AUTH_MAX_SKEW", 300)

        # 解析后的派生值
        self.milvus_mode_resolved: str = ""
        self.warnings: list[str] = []

    # ── 校验 ──
    def validate(self) -> None:
        """启动期校验：不合法直接抛 ConfigError。"""
        self._validate_agent()
        if self.APP_ENV not in ("local", "production"):
            raise ConfigError(f"APP_ENV 只能是 local / production，当前为 {self.APP_ENV!r}")

        if self.MILVUS_MODE not in ("auto", "standalone", "cloud"):
            raise ConfigError(
                f"MILVUS_MODE 只能是 auto / standalone / cloud，当前为 {self.MILVUS_MODE!r}"
            )
        if self.INTERNAL_AUTH_MODE not in ("off", "warn", "enforce"):
            raise ConfigError(
                f"INTERNAL_AUTH_MODE 只能是 off / warn / enforce，当前为 {self.INTERNAL_AUTH_MODE!r}"
            )
        if self.GENERATION_POLICY not in ("legacy", "reuse"):
            raise ConfigError(
                f"GENERATION_POLICY 只能是 legacy / reuse，当前为 {self.GENERATION_POLICY!r}"
            )

        self.milvus_mode_resolved = self._resolve_milvus_mode()
        self._validate_milvus()

        if self.INTERNAL_AUTH_MODE == "enforce" and not self.INTERNAL_API_TOKEN:
            raise ConfigError(
                "INTERNAL_AUTH_MODE=enforce 时必须提供 INTERNAL_API_TOKEN（与 OCRServer 共享的密钥）"
            )
        if self.EMBED_DIM <= 0:
            raise ConfigError(f"EMBED_DIM 必须为正整数，当前为 {self.EMBED_DIM}")

        if self.APP_ENV == "production":
            if self.INTERNAL_AUTH_MODE != "enforce":
                self.warnings.append(
                    f"生产环境 INTERNAL_AUTH_MODE={self.INTERNAL_AUTH_MODE}，"
                    "userId 可被伪造；联调完成后应切到 enforce"
                )

    def _validate_agent(self) -> None:
        reserved = {"public", "auth", "storage", "information_schema", "extensions", "vault", "realtime", "graphql_public"}
        if (
            not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.POSTGRES_SCHEMA)
            or self.POSTGRES_SCHEMA.startswith("pg_") or self.POSTGRES_SCHEMA in reserved
        ):
            raise ConfigError("POSTGRES_SCHEMA 必须是独立的私有 schema 名称，例如 agent_runtime")
        if self.POSTGRES_POOL_MAX_SIZE < 2 or self.POSTGRES_CONNECT_TIMEOUT <= 0:
            raise ConfigError("POSTGRES_POOL_MAX_SIZE 至少为 2，POSTGRES_CONNECT_TIMEOUT 必须为正整数")
        if self.AGENT_MAX_MODEL_CALLS < 1 or self.AGENT_HISTORY_MESSAGES < 2:
            raise ConfigError("AGENT_MAX_MODEL_CALLS 至少为 1，AGENT_HISTORY_MESSAGES 至少为 2")
        if self.AGENT_WEB_SEARCH and not self.TAVILY_API_KEY:
            raise ConfigError("AGENT_WEB_SEARCH=true 时必须配置 TAVILY_API_KEY")
        if not self.POSTGRES_DSN and self.APP_ENV == "production":
            raise ConfigError("生产消息服务必须配置 POSTGRES_DSN，避免生成回复后无法保存消息")
        if not self.POSTGRES_DSN:
            return
        from psycopg.conninfo import conninfo_to_dict
        try:
            params = conninfo_to_dict(self.POSTGRES_DSN)
        except Exception:
            raise ConfigError("POSTGRES_DSN 格式不正确，请使用 PostgreSQL 连接串并对密码进行 URL 编码") from None
        if any(marker in self.POSTGRES_DSN for marker in ("YOUR-PASSWORD", "YOUR_PASSWORD", "[PASSWORD]", "[project-ref]")):
            raise ConfigError("POSTGRES_DSN 中仍有示例占位符，请填写实际数据库连接信息")
        if "pooler.supabase.com" in params.get("host", "") and params.get("port") == "6543":
            raise ConfigError("请使用 Supabase Direct connection 或 Session pooler（5432）；会话锁不支持 Transaction pooler")
        if self.APP_ENV == "production" and self.INTERNAL_AUTH_MODE != "enforce":
            raise ConfigError("启用 PostgreSQL 会话持久化的生产环境必须使用 INTERNAL_AUTH_MODE=enforce")

    def _resolve_milvus_mode(self) -> str:
        if self.MILVUS_MODE != "auto":
            return self.MILVUS_MODE
        # 显式认证信息存在 → 视为托管集群；否则本地无鉴权 standalone
        return "cloud" if (self.MILVUS_TOKEN or self.MILVUS_USER) else "standalone"

    def _validate_milvus(self) -> None:
        host = urlsplit(self.MILVUS_URI).hostname
        remote = not _is_local_host(host)

        if self.milvus_mode_resolved == "cloud":
            if not (self.MILVUS_TOKEN or (self.MILVUS_USER and self.MILVUS_PASSWORD)):
                raise ConfigError(
                    "MILVUS_MODE 解析为 cloud，但没有 MILVUS_TOKEN（或 MILVUS_USER+MILVUS_PASSWORD）"
                )
        else:  # standalone
            if self.MILVUS_TOKEN:
                self.warnings.append(
                    "MILVUS_MODE 为 standalone，但配置了 MILVUS_TOKEN（将被忽略）"
                )
            if remote:
                self.warnings.append(
                    f"standalone 模式却连远程主机 {host}（确认这是你自建集群，而不是 Zilliz Cloud）"
                )

        # 防误连：本地开发默认不允许连生产集群，避免误写
        if self.APP_ENV == "local" and remote and not self.MILVUS_ALLOW_REMOTE_IN_LOCAL:
            raise ConfigError(
                f"APP_ENV=local 却要连接远程 Milvus（{host}）。本地开发请指向 http://localhost:19530。\n"
                "确实需要连远程时，显式设置 MILVUS_ALLOW_REMOTE_IN_LOCAL=1 —— "
                "注意这会往生产集群写集合。"
            )

        if self.milvus_mode_resolved == "cloud" and self.APP_ENV == "production":
            if self.MILVUS_DB and self.MILVUS_DB not in ("default",):
                self.warnings.append(
                    f"云端模式下 MILVUS_DB={self.MILVUS_DB!r} 必须是集群内已存在的库名，"
                    "否则启动时会校验失败（serverless 集群通常只有一个形如 db_xxx 的库；"
                    "留空表示用 Token 默认库）"
                )

    # ── 只读视图（日志与 /health 使用，绝不输出密钥）──
    def to_safe_dict(self) -> dict:
        return {
            "appEnv": self.APP_ENV,
            "envFile": self.ENV_FILE or "(未找到 .env)",
            "host": self.HOST,
            "port": self.PORT,
            # 只报告后端类型，不输出消息、偏好或数据库连接信息。
            "storage": "postgres" if self.POSTGRES_DSN else "none",
            "agent": {
                "checkpointBackend": "postgres" if self.POSTGRES_DSN else "none",
                "webSearch": self.AGENT_WEB_SEARCH,
                "maxModelCalls": self.AGENT_MAX_MODEL_CALLS,
                "pruneCheckpoints": self.POSTGRES_PRUNE_CHECKPOINTS,
            },
            "milvus": {
                "uri": self.MILVUS_URI,
                "mode": self.milvus_mode_resolved,
                "db": self.MILVUS_DB,
                "collection": self.MILVUS_COLLECTION,
                "componentCollection": self.MILVUS_COMPONENT_COLLECTION,
                "tokenConfigured": bool(self.MILVUS_TOKEN),
            },
            "models": {
                "default": self.DEEPSEEK_MODEL,
                "available": self.AVAILABLE_MODELS,
                "apiKeyConfigured": bool(self.DEEPSEEK_API_KEY),
            },
            "embed": {"model": self.EMBED_MODEL, "dim": self.EMBED_DIM},
            "generationPolicy": self.GENERATION_POLICY,
            "outbound": {
                "trustEnvProxy": self.HTTP_TRUST_ENV,
                "explicitProxy": bool(self.MODEL_HTTP_PROXY),
            },
            "internalAuth": {
                "mode": self.INTERNAL_AUTH_MODE,
                "tokenConfigured": bool(self.INTERNAL_API_TOKEN),
            },
            "warnings": self.warnings,
        }


settings = Settings()
