# SnapUI Python 生成服务

本工程使用 FastAPI、LangChain 模型适配、Qwen 嵌入模型和 Milvus，为 SnapUI 提供知识检索与组件代码流式生成。目录名保留为 langchain1.2_tutorial，运行包为 agent_service。

当前使用共享 LangGraph 执行图，在 RAG 上下文中按轮次选择模型，支持搜索/记忆工具循环、会话恢复与跨会话偏好。组件直接复用、自动编译修复和 Subagent 属于后续规划。

## 三个工程与职责

```text
React 前端 → OCRServer（鉴权、提示词、业务数据、OSS、截图理解）
                 │ 内部签名请求；原始输入 + 模型上下文 + 模型配置
                 ▼
             FastAPI（本工程，默认 8000）
                 ├─ Qwen embeddings → Milvus 知识检索
                 ├─ PostgreSQL：消息、事件、检查点、长期记忆
                 └─ 平台 DeepSeek / 用户 OpenAI 兼容模型 ↔ 搜索/记忆工具 → SSE
                 ▼
             OCRServer 记录业务回合并转发 → 前端预览 TSX
```

本服务不连接 MySQL。OCRServer 保存会话归属、标题、计费、作品和附件引用；Python 用 PostgreSQL 保存原始消息、助手回复、事件、检查点和用户长期偏好。生产环境必须配置数据库。API Key 不进入检查点，截图仍归 OSS；Milvus 用于知识库检索。接入与边界见 [Agent 运行时](docs/AGENT_RUNTIME.md)。

配套文档：[OCRServer README](../OCRServer/README.md)、[前端 README](../../front-end/my-tsx-react-app/README.md)。

## 安装与启动

本地直接运行需要 uv、Python 3.13+；Docker 部署无需在宿主机安装它们，详见 [Docker 部署教程](docs/DOCKER_DEPLOY.md)。依赖由 pyproject.toml 声明、uv.lock 锁定。参考 .env.example 填写环境文件，模板不应携带真实凭据。

```bash
uv sync --frozen
# 本地 Milvus：先启动 Docker
docker compose -f deploy/milvus/docker-compose.yml up -d
# 配置 .env / .env.local 后启动
uv run python run.py
```

等价入口与端口覆盖：

```bash
uv run python -m agent_service.run
AGENT_PORT=8001 uv run python run.py
```

默认监听 127.0.0.1:8000；Node 的 AGENT_SERVICE_URL 代码默认端口为 8001，需显式与实际监听地址统一。启动入口设置 reload=False，修改 Python 代码后需重启。

## 入口与模块

启动链：`run.py → agent_service/run.py:main() → Uvicorn → agent_service/main.py:app`。

| 文件或目录 | 职责 |
| --- | --- |
| 根 run.py | 兼容启动壳，调用包内 main |
| agent_service/run.py | 安装日志脱敏、启动 Uvicorn、设置监听 |
| agent_service/main.py | 配置校验、FastAPI 装配、生命周期、异常、路由 |
| api/routers/chat.py | 内部与模型配置验签、开流前校验、自定义模型限流 |
| api/sse.py | SSE 编码 |
| services/chat_service.py | 单轮验证、历史规整、检索、上下文组装与生成 |
| services/messages.py | 文本提取、图片计数、历史转换、检索片段格式化 |
| services/generation.py | 调用模型 astream，提取增量和 usage |
| agent/prompts.py | 缺省 system、知识上下文模板、图片提示 |
| agent/llm/factory.py、cache.py | 模型工厂与按配置隔离的有界缓存 |
| agent/runtime.py | 共享图、动态路由、搜索/记忆工具、流式状态 |
| repositories/checkpoints.py、messages.py | PostgreSQL 连接、会话锁、Store、消息与事件 |
| repositories/embeddings.py | Qwen 文本嵌入 |
| repositories/vector_store.py | Milvus 写入、搜索、统计、本地/云端适配 |
| core/ | 环境配置、内部鉴权、日志脱敏、安全出站、错误类型 |
| tests/ | 模型安全与 HTTP 出站测试 |
| deploy/milvus/ | 本地 Milvus 部署配置 |

表中 api/services/agent/repositories/core 路径均位于 agent_service 下。应用监听前校验配置，并执行 Milvus 连通性检查（等待上限 3 秒，失败记录告警后继续启动；底层同步调用仍受 MILVUS_TIMEOUT 限制）。启动日志包含检查结果与耗时；集合仍按需初始化，并记录数据库、集合和装载检查日志，随后初始化共享执行图及配置的 PostgreSQL 检查点连接。

## 配置

优先级：**进程环境 > .env.<APP_ENV> > .env**。APP_ENV 默认 local，支持 local/production；从配置文件位置定位工程根目录，不依赖启动 cwd。

| 配置 | 用途 |
| --- | --- |
| AGENT_HOST、AGENT_PORT | 默认 127.0.0.1、8000 |
| DEEPSEEK_API_KEY、DEEPSEEK_API_BASE、DEEPSEEK_MODEL | 平台生成模型；代码默认模型名 deepseek-flash |
| AVAILABLE_MODELS | 平台白名单；未知平台模型名回落默认 |
| QWEN_EMBED_API_KEY、QWEN_EMBED_BASE_URL、EMBED_MODEL、EMBED_DIM | 嵌入服务；代码默认模型 qwen3.7-text-embedding、维度 1024 |
| MILVUS_URI、MILVUS_MODE、MILVUS_DB、MILVUS_COLLECTION | 向量库位置、模式、库与集合 |
| MILVUS_TOKEN 或 MILVUS_USER/MILVUS_PASSWORD | 云端认证 |
| RAG_TOP_K | 检索数量，默认 4 |
| POSTGRES_DSN、POSTGRES_SCHEMA | 消息、事件、检查点和长期记忆；生产必填，私有 schema 默认 agent_runtime |
| POSTGRES_POOL_MAX_SIZE、POSTGRES_CONNECT_TIMEOUT | 连接池上限默认 5，连接等待默认 10 秒 |
| POSTGRES_AUTO_SETUP、POSTGRES_PRUNE_CHECKPOINTS | 初始化消息/检查点/Store 表、每轮保留最新检查点；默认均开启 |
| AGENT_WEB_SEARCH、TAVILY_API_KEY | Tavily 搜索；配置密钥时默认开启，也可显式关闭 |
| AGENT_MAX_MODEL_CALLS、AGENT_HISTORY_MESSAGES | 每轮最多 6 次模型调用；模型输入历史目标 40 条，在用户轮次边界裁剪 |
| INTERNAL_AUTH_MODE、INTERNAL_API_TOKEN | 内部鉴权模式及与 Node 共享的密钥 |
| ALLOWED_MODEL_HOSTS | 自定义模型允许域名，与 Node 一致 |
| HTTP_TRUST_ENV、MODEL_HTTP_PROXY | 平台出站配置；自定义模型另走安全客户端 |

以上模型名是代码默认值，实际可用名称由配置的供应商决定。嵌入维度需与集合 schema 一致。

本地默认 standalone；local 模式阻止意外连接远程 Milvus，确需远程时设置 MILVUS_ALLOW_REMOTE_IN_LOCAL=1。云端使用已有库，MILVUS_DB 需填写集群实际库名。鉴权支持 off/warn/enforce，生产应使用 enforce；自定义模型始终需要有效签名，即使平台调用配置为 off。

## 单轮对话流程

1. Node 核验用户、会话、作品与附件，预留附件引用，组装可信 system 和组件上下文，并生成稳定消息 UUID。
2. 有截图时，Node 用平台 MiMo 生成或复用描述，再把文字交给 Python。本服务不读取 OSS，也不识别图片像素。
3. chat.py 校验内部身份与敏感配置签名，调用 validate_request；空消息在 SSE 开流前返回 HTTP 错误。
4. Python 保存独立的原始输入，恢复检查点；缺少检查点时从 PostgreSQL 消息重建历史。
5. 非空文本进行 Milvus 检索；异常时记录 degraded 并继续生成。命中片段以临时参考知识加入本轮模型输入，不重复保存到会话消息。
6. 使用上游 system 或缺省提示词组装消息，按平台 model 或内部 modelConfig 选择模型。
7. 共享执行图读取用户偏好，按本轮路由获取缓存模型，执行搜索/记忆工具循环，流式返回文本并累加用量；Python 持久化回复、状态、事件和检查点，Node 记录业务回合元数据。

当前 RAG 每轮仍调用生成模型，不直接返回相似组件。Python 当前默认报告 fresh；明确调整的最终模式由 Node 按其上下文记录。

## HTTP 与 SSE

| 方法与路径 | 用途 |
| --- | --- |
| GET / | 服务发现 |
| GET /health | 实际探测 Milvus 和已配置的 PostgreSQL，返回 ok/degraded |
| GET /health?deep=1 | 额外调用嵌入模型并检查维度 |
| GET /api/models | 平台模型列表和默认值 |
| POST /api/chat | 流式生成 |
| GET /api/agent/sessions/:id/messages | 内部消息游标分页 |
| GET /api/agent/sessions/:id/messages/:messageId | 内部精确消息查询 |
| DELETE /api/agent/sessions/:id | 联动清理消息、事件、检查点 |
| GET/PUT/DELETE /api/agent/memories[/:key] | 当前用户长期偏好 |
| POST /api/knowledge/ingest | 写入 texts 或 documents |
| POST /api/knowledge/search | 检索 query，可指定 top_k |
| GET /api/knowledge/stats | 集合统计 |
| GET /docs | FastAPI 接口文档 |

历史、模型配置管理、截图上传、作品 CRUD 由 OCRServer 提供。

Node 下发的普通请求：

```json
{
  "conversationId": "会话ID",
  "userId": "已鉴权用户ID的十进制字符串",
  "message": "用户文字、必要组件上下文及截图描述",
  "originalMessage": "用户原始输入或稳定附件内容块",
  "messageId": "用户消息UUID",
  "assistantMessageId": "助手消息UUID",
  "system": "由OCRServer组装的系统提示词"
}
```

平台调用可加 model；自定义调用由 Node 内部附带 modelConfig，提供身份和配置签名。Python 不解析 modelProfileId、不读取保存的密钥。attachments 组件附件字段尚未启用，不等同于 Node 已实现的截图 attachmentId。

```text
event: start
data: {"conversationId":"..."}

event: delta
data: {"text":"..."}

event: done
data: {"conversationId":"...","stopReason":"stop","model":"...","usage":null,"mode":"fresh","reused":false,"degraded":false}
```

生成失败返回 error 事件；开流前的参数和权限错误返回 HTTP JSON。模型未提供 usage 时保持 null。

## 自定义模型安全

Node 加密保存密钥，Python 在内部请求中使用。自定义地址限定允许域名、HTTPS 和公共 IP；DNS 解析后固定连接 IP，保留 TLS SNI/Host，禁止重定向与环境代理。模型缓存隔离凭据，淘汰时关闭客户端。

每用户最多两轮并发、每分钟 20 次，是单实例内存限流。OpenAI 兼容协议不代表工具调用和多模态能力已验证。详见 [模型配置说明](../OCRServer/MODEL_PROFILES.md)。

## 验证与部署

```bash
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false uv run python -m unittest discover -s tests -q
uv run python -m agent_service.ops.verify_storage
uv run python -m agent_service.ops.verify_messages
curl http://127.0.0.1:8000/health
```

深度检查会调用嵌入服务。开启 enforce 后，对话和知识库接口需内部签名，完整联调通过 Node，浏览器不直接提交内部凭据。

生产环境 Python 服务通过 Docker 独立部署；[Node PM2 配置](../OCRServer/ecosystem.config.cjs) 仅管理 OCRServer。根 docker-compose.yml 仅部署 Python app，本地 Milvus 使用独立 deploy/milvus 栈。容器部署使用独立的 `.env.docker`，默认启用内部鉴权、仅绑定宿主机回环地址；完整命令、OCRServer 联调与回退见 [Docker 部署教程](docs/DOCKER_DEPLOY.md)。

后续规划见 [REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) 与 [DESIGN.md](docs/DESIGN.md)。这些文档含规划内容；当前功能以运行代码和本 README 的实现说明为准。


## Model client cache lifecycle

Use `with get_model_cache().acquire(spec) as model:` for the entire streaming request. The context releases its reference on completion, error, cancellation or explicit stream closure. Raw untracked `get()` access is no longer supported.

The cache retains up to 64 reusable entries with a 30-minute TTL from creation. LRU eviction, expiry and `clear()` immediately retire entries from reuse. Active requests keep their old client until the last lease releases; only then are the HTTP clients closed. Consequently the total number of live clients can temporarily exceed the reuse limit. This cache limit is not a request concurrency limit.

Bookkeeping and same-key construction are protected by a lock. Releasing an old entry cannot remove its same-key replacement. Sync and async connection cleanup failures are logged without failing a completed request. Streaming wrappers explicitly close inner generators so early close also releases model leases.

Tests (no real model calls): `.venv/bin/python -B -m unittest discover -s tests -p 'test_model_*.py'`. Includes shared active clients, the 65th entry, TTL replacements, cancellation, upstream errors, generator closure and concurrent threads.
