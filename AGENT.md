# 语言偏好

- 所有面向用户的回复均使用中文。
- 所有新增或修改的代码注释均使用中文。
- 项目文档默认使用中文。
- 除非用户明确要求使用其他语言，否则始终遵循以上语言偏好。

# 项目概述

- 本项目是 SnapUI Agent 服务（Python 工程）：一个基于 FastAPI 的多模态生成型 agent。
- 使用 DeepSeek 作为基础模型生成前端组件；
- 使用 Qwen 嵌入模型 + Milvus（本地自托管或 Zilliz Cloud 托管）支持 RAG；
- 本服务使用共享 LangGraph 执行图、动态模型路由、PostgreSQL 消息/事件/检查点和用户长期偏好 Store。业务会话目录、组件资产、计费和模型配置归 OCRServer；PostgreSQL 消息是历史来源，生产必须配置数据库。
- 通过 SSE 与上游后端（OCRServer）通信。

# 运行环境

- Python 3.13，由 [uv](https://docs.astral.sh/uv/) 管理，使用项目内 `.venv` 虚拟环境（见 `.python-version` 与 `pyproject.toml`）。
- 依赖声明在 `pyproject.toml` 的 `[project].dependencies`，锁定文件为 `uv.lock`；新增 / 移除依赖使用 `uv add` / `uv remove`。
- 安装 / 同步依赖：`uv sync`（在项目根目录执行）。
- 从项目根目录启动：`uv run python run.py`（兼容壳）或 `uv run python -m agent_service.run`；PM2 指向 `agent_service/run.py`。
- 配置优先级：进程环境 > `.env.<APP_ENV>` > `.env`（因此 `AGENT_PORT=8001 uv run python run.py` 可换端口做蓝绿）。
- 主要依赖：`fastapi`、`uvicorn`、`langchain`、`langchain-core`、`langchain-deepseek`、`langchain-openai`、`langchain-community`、`pymilvus`、`pydantic`、`python-dotenv`，以及 `langgraph`、`langgraph-checkpoint-postgres`、`psycopg[binary,pool]`。

# 项目结构（P0 重写后：`agent_service/` 包）

- 入口：`agent_service/run.py`（PM2 目标）；根 `run.py` 为兼容壳。**旧平铺实现已删除**（P0 重写验证后清理）。
- `agent_service/core/`：`config.py`（分环境 + 启动期校验）、`logging.py`（日志脱敏）、`http.py`（出站代理策略）、`security.py`（内部鉴权 HMAC）、`errors.py`（错误类型与安全描述）。
- `agent_service/api/`：`routers/{system,chat,knowledge}.py`、`sse.py`（SSE 编码，帧格式与旧版逐字节一致）、`deps.py`（身份解析）。
- `agent_service/services/`：`chat_service.py`（一轮对话编排）、`generation.py`（生成流水线）、`messages.py`（消息转换）。
- `agent_service/agent/`：`prompts.py`、`runtime.py`（共享图与搜索/记忆工具）、`llm/{factory,cache}.py`（模型工厂 + LRU/TTL 缓存）。
- `agent_service/repositories/`：`embeddings.py`、`vector_store.py`（本地 standalone / 生产 Zilliz 双环境）。**C3 已删除** `mysql.py`、`sessions.py`、`turns.py` 与 `agent_service/migrations/`（建表 DDL 迁至 `OCRServer/migrations/002_agent_domain.sql`）。
- **当前存储边界（2026-10-26）**：Python 在独立私有 schema 保存消息、事件、检查点和用户长期偏好；OCRServer 不再写 MySQL 消息。会话删除由 OCRServer 调用 `DELETE /api/agent/sessions/:id` 联动清理消息/事件/检查点，再删业务元数据；长期偏好单独删除。
- **错误码**：400 参数 / 401 身份 / 503 向量库不可用 / 500 兜底（501 已随 C3 移除）。
- PostgreSQL 使用 `POSTGRES_DSN`（Supabase Direct/Session pooler，密码需 URL 编码）；连接串不得输出。`POSTGRES_SCHEMA` 默认 `agent_runtime`，禁止使用 public/auth/storage。配置说明见 `docs/AGENT_RUNTIME.md`。
- 部署：Python 使用 `Dockerfile` + `docker-compose.yml`；OCRServer 的 PM2 只管理 Node。切换消息架构须按 `docs/AGENT_RUNTIME.md` 安排维护窗口，旧 Node 不可与新表结构混跑。

# 开发约定

- 代码注释与文档使用中文。
- 回归测试：`LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false uv run python -m unittest discover -s tests -q`；真实存储验证：`uv run python -m agent_service.ops.verify_storage`；跨服务联调：`uv run python -m agent_service.ops.verify_messages`（开发 MySQL + Supabase，模拟模型）。
- 不得在回复、代码、日志或文档中泄露 `.env` 内的 API 密钥。
