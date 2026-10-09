# SnapUI Agent 服务（Python 工程）

这是一个支持「图片 + 文字组合」的生成型多模态产品的 **Agent 服务**：用户上传截图 + 文字，快速产出设计稿对应的前端组件，并在前端通过 `babel/standalone` 实时预览、支持复制源码。

本服务是三个工作区中的 **Python 工程**，定位为下游的 agent 服务：

- 使用 **FastAPI** 对外提供 HTTP 接口，与上游 **后端工程**（`/Users/CodeGod/backend/OCRServer`）通信；
- **存储按域分工（C2）**：会话数据（`conversations` / `messages`）归 OCRServer 的 **MySQL**，本服务**只读**；组件资产、回合元数据、模型配置等自有域表由本服务拥有（详见 `docs/DESIGN.md`）；
- 使用 **Milvus** 管理向量数据，支持 **RAG**；
- 基础模型使用 **DeepSeek**，嵌入模型使用 **Qwen**（`qwen3.7-text-embedding`，1024 维）。

## 三个工作区串联关系

```
┌──────────────┐     /api/chat(SSE)      ┌────────────────────┐   OCR/多模态   ┌────────────────┐
│  前端工程      │ ──────────────────────▶ │  后端工程 OCRServer │ ─────────────▶ │ MiMo 视觉/OCR   │
│ my-tsx-react- │                        │  (Node/Express)     │                └────────────────┘
│    app        │ ◀────────────────────── │   鉴权/积分/历史     │
└──────────────┘      SSE 流式返回        └─────────┬──────────┘
                                                    │ 转发文本(OCR 结果 + 用户文字)
                                                    ▼
                                         ┌────────────────────┐
                                         │  Python 工程(本服务) │
                                         │  FastAPI Agent 服务 │
                                         │  DeepSeek + RAG     │
                                         │  PostgreSQL 会话     │
                                         │  Milvus 向量库       │
                                         └────────────────────┘
```

- **前端工程**：`/Users/CodeGod/front-end/my-tsx-react-app`（React + TSX，`babel/standalone` 实时预览）。
- **后端工程**：`/Users/CodeGod/backend/OCRServer`（Node/Express，JWT 鉴权、积分、历史记录，默认 `127.0.0.1:3000`）。
- **Python 工程**：本项目根目录（`langchain1.2_tutorial`），默认 `127.0.0.1:8000`。

> 说明：DeepSeek 为基础文本模型，无法直接读取图片像素。截图理解（OCR / 视觉描述）由后端工程的 MiMo 视觉模型完成，后端把「OCR 结果 + 用户文字」以文本转发给本服务；本服务负责 RAG 检索与组件代码生成。

## 目录结构

```
agent_service/         # 服务实现（P0 重写后的包）
  run.py               # 真实入口（PM2 指向这里）
  main.py              # FastAPI 装配：配置校验 / 生命周期 / 异常处理
  core/                # config（分环境+启动校验）· logging（脱敏）· http（出站代理）· security（内部鉴权）· errors
  api/                 # routers/{system,chat,history,knowledge} · sse · deps
  services/            # chat_service（对话编排）· generation（生成流水线）· messages（消息转换）
  agent/               # prompts · llm/{factory,cache}
  repositories/        # mysql（双池+迁移器）· sessions（会话域只读）· turns · embeddings · vector_store
  migrations/          # 自有域建表 SQL（启动时幂等应用）
run.py                 # 兼容壳：转调 agent_service/run.py
docs/                  # REFACTOR_PLAN.md（状态/决策）· DESIGN.md（设计）· archive/
pyproject.toml uv.lock # 依赖清单与锁定
.env / .env.local      # 密钥（基座）与本地覆盖（后者已 gitignore）
Dockerfile docker-compose.yml README.md AGENT.md CLAUDE.md
```

## 快速开始

前置条件（已满足）：

- 已安装 uv（Python 3.13 由 uv 自动管理，见 `.python-version`）；
- Docker 容器 `postgre`（PostgreSQL，`localhost:5432`）与 `milvus-standalone`（Milvus，`localhost:19530`）已运行；
- 项目根目录 `.env` 已声明 `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE`、`QWEN_EMBED_API_KEY`、`QWEN_EMBED_BASE_URL`。

```bash
uv sync   # 同步依赖（按 pyproject.toml + uv.lock 安装到 .venv）

# 在项目根目录 langchain1.2_tutorial 下启动（两种写法等价）
uv run python run.py
uv run python -m agent_service.run

# 换端口/做蓝绿：进程环境优先于 .env，因此可直接覆盖
AGENT_PORT=8001 uv run python run.py
```

> 关键环境变量（完整见 `.env.example`）：
> `MYSQL_DSN`（自有域，读写）· `MYSQL_RO_DSN`（会话域，**只读**）· `LEGACY_DB_TIMEZONE`（OCRServer 的 DATETIME 是本地墙钟）
> `MILVUS_URI` / `MILVUS_TOKEN` / `MILVUS_MODE`（`auto|standalone|cloud`）· `MILVUS_DB`（云端须留空或用集群内既有库名）
> `INTERNAL_AUTH_MODE`（`off|warn|enforce`）+ `INTERNAL_API_TOKEN` · `APP_ENV`（`local|production`）· `HTTP_TRUST_ENV`/`MODEL_HTTP_PROXY`

启动后可访问：

- `GET http://127.0.0.1:8000/` —— 服务发现（列出全部接口）；
- `GET http://127.0.0.1:8000/health` —— 健康检查；
- 接口文档 `GET http://127.0.0.1:8000/docs`（FastAPI 自带 Swagger）。

## 环境变量

密钥统一从 **项目根目录** `langchain1.2_tutorial/.env` 加载；以下基础设施参数内置了与 Docker 默认配置一致的默认值，可按需通过环境变量覆盖（详见 `.env.example`）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | （.env 提供） | DeepSeek API Key |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com/v1` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 基础模型 |
| `QWEN_EMBED_API_KEY` | （.env 提供） | Qwen 嵌入 Key |
| `QWEN_EMBED_BASE_URL` | （.env 提供） | Qwen 嵌入地址 |
| `EMBED_MODEL` | `qwen3.7-text-embedding` | 嵌入模型 |
| `EMBED_DIM` | `1024` | 向量维度 |
| `POSTGRES_DSN` | `postgresql://postgres:admin@localhost:5432/langchain_db` | 会话库 |
| `MILVUS_URI` | `http://localhost:19530` | 向量库地址 |
| `MILVUS_DB` | `snapui_rag` | 向量库数据库名 |
| `MILVUS_COLLECTION` | `design_knowledge` | 向量集合名 |
| `RAG_TOP_K` | `4` | 检索 top-k |
| `AGENT_HOST` / `AGENT_PORT` | `127.0.0.1` / `8000` | 服务监听 |

## API 说明

### 1. 对话生成（SSE 流式）

`POST /api/chat`

请求体（与上游后端 `/api/chat` 契约一致）：

```json
{
  "conversationId": "可选，缺省自动生成 UUID",
  "message": "字符串，或内容块数组",
  "system": "可选系统提示词",
  "model": "可选，缺省用 DEEPSEEK_MODEL",
  "userId": "可选，上游鉴权后的用户标识"
}
```

`message` 支持两种形态：

- 纯文本：`"帮我生成一个登录卡片"`；
- 多模态内容块数组（与前端一致）：

```json
[
  { "type": "image", "source": { "type": "base64", "media_type": "image/png", "data": "<base64>" } },
  { "type": "text", "text": "把它做成一个登录卡片组件" }
]
```

SSE 事件协议（与后端 OCRServer 保持一致）：

```
event: start   data: {"conversationId":"..."}
event: delta   data: {"text":"..."}
event: delta   data: {"text":"..."}
event: done    data: {"conversationId":"...","stopReason":"stop","model":"...","usage":null}
```

异常时：

```
event: error   data: {"error":"...","type":"internal_error"}
```

### 2. 会话历史（PostgreSQL）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/history?page=&pageSize=&userId=` | 分页列出会话 |
| `GET` | `/api/history/{conversationId}?userId=` | 会话详情（含消息） |
| `POST` | `/api/history` | 创建会话 `{"title": "", "userId": ""}` |
| `DELETE` | `/api/history/{conversationId}?userId=` | 删除会话 |
| `DELETE` | `/api/chat/{conversationId}?userId=` | 清除会话 |

### 3. 知识库（RAG）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/knowledge/ingest` | 写入知识 `{"texts":[...]}` 或 `{"documents":[{"text":"...","source":"..."}]}` |
| `POST` | `/api/knowledge/search` | 检索 `{"query":"...","top_k":4}` |
| `GET` | `/api/knowledge/stats` | 集合统计 |

## 快速验证（curl 示例）

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 写入知识库
curl -X POST http://127.0.0.1:8000/api/knowledge/ingest \
  -H 'Content-Type: application/json' \
  -d '{"texts":["按钮组件应包含圆角、主色背景与点击态","卡片组件包含标题、图片与操作按钮"]}'

# 检索
curl -X POST http://127.0.0.1:8000/api/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"如何设计按钮组件","top_k":2}'

# 对话（SSE 流式，纯文本）
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"帮我生成一个登录卡片组件"}'
```

## 与后端的对接建议

后端 OCRServer 的 `services/chat.js` 目前直接调用 MiMo。接入本服务时，可把「OCR / 视觉描述」之后的结果转发到本服务：

```js
// 伪代码示例（非本次改动内容）
const resp = await fetch('http://127.0.0.1:8000/api/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    conversationId,
    userId: req.user.id,
    message: ocrResultText + '\n' + userText,  // 或原样转发内容块数组
    system,
  }),
});
// 将 resp.body（text/event-stream）透传给前端即可
```

## 设计说明

- **模型实例化**：DeepSeek 使用 `init_chat_model(model='deepseek-v4-flash', model_provider='deepseek')`；嵌入模型使用 `init_embeddings(model='openai:qwen3.7-text-embedding', ...)`，与 `11_RAG/03-文档嵌入模型.ipynb` 第二个 cell 一致。
- **会话存储（C2 变更）**：会话域归 OCRServer 的 MySQL，本服务用**只读账号**读取（`MYSQL_RO_DSN`）；创建/删除会话的接口返回 **501**，由 OCRServer 承担。本服务另有自有域表（`components*`、`conversation_turns`、`model_profiles`）。
- **RAG 流水线**：对话文本 → 向量检索（Milvus，COSINE）→ 将命中片段作为「参考知识」注入提示词 → DeepSeek 流式生成。
- **图像处理**：DeepSeek 为纯文本模型；本服务完整接收多模态内容块格式，图片的 OCR / 视觉描述由上游后端完成，未配置视觉模型时仅做占位提示，不影响文本生成。
