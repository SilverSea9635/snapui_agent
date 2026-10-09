# 语言偏好

- 所有面向用户的回复均使用中文。
- 所有新增或修改的代码注释均使用中文。
- 项目文档默认使用中文。
- 除非用户明确要求使用其他语言，否则始终遵循以上语言偏好。

# 项目概述

- 本项目是 SnapUI Agent 服务（Python 工程）：一个基于 FastAPI 的多模态生成型 agent。
- 使用 DeepSeek 作为基础模型生成前端组件；
- 使用 Qwen 嵌入模型 + Milvus（本地自托管或 Zilliz Cloud 托管）支持 RAG；
- 会话数据存 MySQL（OCRServer 的库 `OCRDB`：`conversations`/`messages` 归它，本服务**只读**；自有域的表由启动期迁移器幂等创建，PG 已退役）；
- 通过 SSE 与上游后端（OCRServer）通信。

# 运行环境

- Python 3.13，由 [uv](https://docs.astral.sh/uv/) 管理，使用项目内 `.venv` 虚拟环境（见 `.python-version` 与 `pyproject.toml`）。
- 依赖声明在 `pyproject.toml` 的 `[project].dependencies`，锁定文件为 `uv.lock`；新增 / 移除依赖使用 `uv add` / `uv remove`。
- 安装 / 同步依赖：`uv sync`（在项目根目录执行）。
- 从项目根目录启动：`uv run python run.py`（兼容壳）或 `uv run python -m agent_service.run`；PM2 指向 `agent_service/run.py`。
- 配置优先级：进程环境 > `.env.<APP_ENV>` > `.env`（因此 `AGENT_PORT=8001 uv run python run.py` 可换端口做蓝绿）。
- 主要依赖：`fastapi`、`uvicorn`、`langchain`、`langchain-core`、`langchain-deepseek`、`langchain-openai`、`langchain-community`、`asyncmy`、`pymilvus`、`pydantic`、`python-dotenv`。

# 项目结构（P0 重写后：`agent_service/` 包）

- 入口：`agent_service/run.py`（PM2 目标）；根 `run.py` 为兼容壳。**旧平铺实现已删除**（P0 重写验证后清理）。
- `agent_service/core/`：`config.py`（分环境 + 启动期校验）、`logging.py`（日志脱敏）、`http.py`（出站代理策略）、`security.py`（内部鉴权 HMAC）、`errors.py`（错误类型与安全描述）。
- `agent_service/api/`：`routers/{system,chat,history,knowledge}.py`、`sse.py`（SSE 编码，帧格式与旧版逐字节一致）、`deps.py`（身份解析）。
- `agent_service/services/`：`chat_service.py`（一轮对话编排）、`generation.py`（生成流水线）、`messages.py`（消息转换）。
- `agent_service/agent/`：`prompts.py`、`llm/{factory,cache}.py`（模型工厂 + LRU/TTL 缓存）。
- `agent_service/repositories/`：`mysql.py`（双池 + 迁移器）、`sessions.py`（会话域只读）、`turns.py`、`embeddings.py`、`vector_store.py`（本地 standalone / 生产 Zilliz 双环境）。
- `agent_service/migrations/*.sql`：自有域建表，启动时按文件名幂等应用。
- **存储域边界（C2）**：`conversations`/`messages` 归 OCRServer（本服务只读）；组件资产、回合元数据、模型配置归本服务。禁止跨域写、跨服务外键、跨服务 JOIN。
- **错误码**：400 参数 / 401 身份 / 404 不存在 / 501 会话写（归 OCRServer）/ 503 向量库不可用 / 500 兜底。
- 项目根目录 `.env` 保存密钥；`.env.<APP_ENV>` 叠加覆盖（`MILVUS_*`、`DEEPSEEK_*`、`QWEN_*`、`MYSQL_DSN`、`LEGACY_DB_TIMEZONE`、`INTERNAL_API_TOKEN` 等）。
- 部署：PM2（`OCRServer/ecosystem.config.cjs` 的 `agent-service`，解释器 `.venv/bin/python`）；`Dockerfile` + `docker-compose.yml` 仍可用于本地。

# 开发约定

- 代码注释与文档使用中文。
- 暂无正式测试框架。
- 不得在回复、代码、日志或文档中泄露 `.env` 内的 API 密钥。
