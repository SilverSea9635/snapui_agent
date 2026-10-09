# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 语言偏好

- 所有回复使用中文
- 代码注释使用中文

## Project Overview

SnapUI Agent Service — a FastAPI-based multimodal generation agent:
- Uses DeepSeek as the base model to generate React frontend components.
- Uses Qwen embedding model + Milvus (self-hosted or Zilliz Cloud) for RAG.
- Session storage lives in **OCRServer's MySQL** (this service reads `conversations`/`messages` **read-only**); its own domain (component assets, turn metadata, model profiles) is a new set of MySQL tables it owns.
- Streams responses over SSE to the upstream OCRServer backend.

## Environment

- **Python**: 3.13, managed by [uv](https://docs.astral.sh/uv/) in the project-local `.venv` (see `.python-version` and `pyproject.toml`).
- **Dependencies**: declared in `pyproject.toml` → `[project].dependencies`, locked in `uv.lock`; add/remove with `uv add` / `uv remove`.
- **Sync**: `uv sync` (from project root)
- **Run**: `uv run python run.py`（兼容壳）或 `uv run python -m agent_service.run`；PM2 指向 `agent_service/run.py`
- **端口/环境覆盖**: 优先级为「进程环境 > `.env.<APP_ENV>` > `.env`」，所以 `AGENT_PORT=8001 uv run python run.py` 可用于蓝绿换端口
- **Key dependencies**: `fastapi`, `uvicorn`, `langchain`, `langchain-core`, `langchain-deepseek`, `langchain-openai`, `langchain-community`, `asyncmy`（MySQL，C2 后取代 psycopg）, `pymilvus`, `pydantic`, `python-dotenv`
- **本地缓存**: 受限环境下 uv 写不了全局缓存，用工程内 `.uv-cache/`（已 gitignore）；升级依赖时先 `unset all_proxy`

## Architecture（P0 重写后：`agent_service/` 包）

- 入口：`agent_service/run.py`（PM2 目标）；根 `run.py` 只是兼容壳（转调前者）。**旧平铺实现已删除**（P0 重写并验证后清理；仓库当时尚无 commit，故旧代码不可恢复）。
- `agent_service/core/` — `config.py`（分环境 + 启动期校验）、`logging.py`（脱敏）、`http.py`（出站代理策略）、`security.py`（内部鉴权 HMAC）、`errors.py`
- `agent_service/api/` — `routers/{system,chat,history,knowledge}.py`、`sse.py`（SSE 编码）、`deps.py`（身份解析）
- `agent_service/services/` — `chat_service.py`（一轮对话编排）、`generation.py`（生成流水线）、`messages.py`（消息转换）
- `agent_service/agent/` — `prompts.py`、`llm/{factory,cache}.py`；P2 会加 `graph.py`/`tools.py`
- `agent_service/repositories/` — `mysql.py`（双池 + 迁移器）、`sessions.py`（会话域**只读**）、`turns.py`、`embeddings.py`、`vector_store.py`（Milvus 双环境）
- `agent_service/migrations/*.sql` — 自有域建表（幂等，启动时自动应用）
- 模块间使用相对导入（包内）；测试见 `tests/`（P5）
- **存储域边界（C2）**：会话域（`conversations`/`messages`）归 OCRServer，本服务**只读**；自有域（组件资产、回合元数据、模型配置）才可写。禁止跨域写 / 跨服务外键 / 跨服务 JOIN。
- **错误码约定**：400 参数、401 身份、404 不存在、501 会话写（归 OCRServer）、503 向量库不可用、500 兜底。
- `.env` 为基座、`.env.<APP_ENV>` 叠加（`.env.local` 已 gitignore，用于本地把 Milvus 指向 standalone）。
- 部署：PM2（`OCRServer/ecosystem.config.cjs` 的 `agent-service` 进程，解释器为 `.venv/bin/python`）；Dockerfile/compose 仍可用于本地。
- 本地 Milvus：compose 栈 `deploy/milvus/`（Docker Desktop 里显示为项目 `milvus`，数据在命名卷 `milvus-data`），`docker compose -f deploy/milvus/docker-compose.yml up -d`；旧 `/Users/CodeGod/milvus/standalone_embed.sh` + bind mount 链路已废弃。

## Conventions

- Comments and documentation in Chinese.
- No formal test framework.
- Never leak `.env` secrets in replies, code, logs, or documentation.
