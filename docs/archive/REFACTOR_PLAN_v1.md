# SnapUI Python 工程重构说明（待确认，确认后再动手）

> 版本：v1 草案　|　范围：本仓库（`/Users/CodeGod/backend/langchain1.2_tutorial`）
> 目标：把当前「一次性生成组件」的服务，升级为「组件资产化 + Agent 工具化 + 用户自带模型」的工程。
> 本文只描述方案，**不含已执行的改动**。第 10 节列出必须由你拍板的决策项。

---

## 0. 结论摘要

| 你的需求 | 本方案的核心做法 | 主要新增 |
| --- | --- | --- |
| ① 组件原子化 + 命中即返回 | 建独立组件资产库（**存储：单一 MySQL，源码为唯一真相源，见 §13**；Milvus 只做检索索引）；生成前先做**意图路由 + 三档策略**（直接复用 / 参考生成 / 全量生成） | `ui_components` 向量集合、`components` 等 7 张表、`/api/components/*`、回填与阈值校准脚本 |
| ② 进阶为 AI Agent + 作品广场 | 用已安装的 `langchain.agents.create_agent`（LangGraph）装配工具链；新增「附件」机制把组件源码引入对话上下文；广场提供发布/检索/版本/fork/点赞 | Agent 图 + 5 个工具、`message_attachments`、广场 API 与合规静态校验 |
| ③ 用户自定义 APIKey/BaseURL/Model | 模型抽象成「每请求模型配置」，两条落地路径（内联传参 / 加密落库）；补上 SSRF 防护、密钥脱敏、实例缓存与能力降级 | `/api/model-profiles/*`、`llm/factory.py`、`llm/cache.py`、`core/security.py` |

三条需求有一个共同的前置：**服务必须先有身份与信任边界**。现状 Python 服务的 `userId` 完全来自请求体，任何人可伪造（见 1.3）。一旦引入「用户自有密钥」和「用户作品公开可见」，这个问题会直接变成越权与密钥泄露，所以它被放进 P0 阶段先做。

**范围边界**：本方案主体在本仓库；广场页面、JWT 鉴权、积分规则属于前端工程与 OCRServer 工程，本文只给出**接口契约与对接清单**（4.6、5.6），不代改那两个仓库。

---

## 1. 现状盘点

### 1.1 已验证的事实（本次实测，非推测）

| 项 | 结论 | 验证方式 |
| --- | --- | --- |
| 代码规模 | 8 个模块平铺根目录，共 1016 行 | `wc -l` |
| LangChain / LangGraph | `langchain 1.3.18`、`langgraph 1.2.11`（作为传递依赖已安装），`from langchain.agents import create_agent` **可用**，`langgraph.checkpoint.memory.InMemorySaver` 可用 | venv 内导入 |
| PG checkpointer | `langgraph.checkpoint.postgres` **未安装**（需新增 `langgraph-checkpoint-postgres`） | venv 内导入失败 |
| pymilvus | `3.0.1`，`create_schema` / `prepare_index_params` / `hybrid_search` / `AsyncMilvusClient` 均可用 | venv 内自省 |
| 嵌入模型 | `qwen3.7-text-embedding` 在配置的端点上**存在**，实测返回 **1024 维**，与 `EMBED_DIM` 一致 | 真实调用 `/embeddings` |
| 对话模型 | 配置端点 `/models` 实际返回 `deepseek-flash`、`deepseek-v4-pro` | 真实调用 `/models` |
| ⚠️ 默认模型名错误 | `config.py` 默认 `DEEPSEEK_MODEL=deepseek-v4-flash`、`AVAILABLE_MODELS=deepseek-v4-flash,...`，而端点侧真实 id 是 `deepseek-flash` → 默认模型调用会失败 | 同上，两处对照 |
| ⚠️ Milvus 当前不可达 | `.env` 的 Zilliz 主机名 DNS 解析失败（`gaierror`），无法现场验证集合操作 | `socket.gethostbyname` + 连接尝试 |
| ⚠️ 本机无法启动服务（PG 路径） | `POSTGRES_DSN` 实际指向 `localhost:5432`。**此前记录为「主机是 postgres（compose 内部名）」是我的解析错误**（正则抓到了用户名而非主机名，已用 `urlsplit` 复核）。真正原因是 PG 由 Docker 提供、而 Docker 守护进程当前未运行 → `session_store.open()` 抛 `PoolTimeout` | `urlsplit` 解析 DSN + psycopg 直连（127.0.0.1/::1 均 Connection refused） |
| ⚠️ 出站被死代理拦截 | 环境变量 `all_proxy=socks5://127.0.0.1:7890`，该端口无监听；`httpx`/LangChain 默认读环境代理 → 走代理必失败（本次实测均为 `trust_env=False` 直连成功） | curl 与 httpx 对照 |
| ✅ 本机 MySQL 可用（新增核实） | `127.0.0.1:3306` 直连成功，版本 **8.4.9**，库 `OCRDB`；表：`users`、`conversations`、`messages`、`user_sign_ins`、`credit_transactions`、`works`、`orders` | Node `mysql2` 用 OCRServer `.env.development` 的配置真实连接并 `SHOW TABLES` |
| ⚠️ `messages` 两处硬约束（新增核实） | ① `content` 是 **`TEXT`（上限 65535 字节）**，而 OCRServer 会写入含 base64 图片的原始消息 → §13.5 的风险**已确认在 schema 层面成立**（当前仅 2 行、max 42 字节、无 base64，属尚未触发）；② `user_id` 是 **`INT`** 且外键指向 `users.id`，而本服务现在用的是 `VARCHAR(64)`（含 `'anonymous'`）→ 双方用户标识类型不一致，C2 后必须统一（见 §13.5） | `SHOW CREATE TABLE messages` |
| ⚠️ `works` / `orders` 表已存在但无人使用（新增核实） | `works(user_id,title,description,source_code MEDIUMTEXT,preview_image,tags JSON,is_public,…)`、`orders`：两张表都在 `OCRDB` 里，行数均为 0，且 OCRServer 的 `routes/`、`services/` 中**没有任何 SQL 引用它们** → 疑似为「作品广场」预留或历史遗留。**直接决定 §4.4 的广场表设计**（见 §4.4 注） | `SHOW CREATE TABLE works` + OCRServer 全仓 SQL 检索 |
| ⚠️ Docker 守护进程当前未运行 | `docker context` 指向 Docker Desktop，但 `~/.docker/run/docker.sock`（及 `/var/run/docker.sock` 等常见路径）均不存在，`docker ps` 无法连接 → **PG 容器与 Milvus 容器此刻都是停的**（容器定义存在，不代表在跑） | 逐个检查 socket 路径 + `docker ps` |
| ℹ️ 本地 Milvus 是自托管 standalone | `/Users/CodeGod/milvus/`：`standalone_embed.sh` + `embedEtcd.yaml` + `user.yaml` + `volumes/`（官方 Milvus standalone 嵌入脚本，Docker 运行）；注意本服务 `.env` 里的 `MILVUS_URI` 目前指向 **Zilliz Cloud**，本地开发需要改指 `http://localhost:19530` | 目录内容 |
| ⚠️ 生产尚未部署本服务，但开关已开 | `OCRServer/.env.development` 与 `.env.production` 均为 `AGENT_SERVICE_ENABLED=true`（`.env.example` 为 `false`）；而生产上本服务**未部署**（你已确认）→ 现在把 OCRServer 发到生产，`/api/chat` 会因连不上 `127.0.0.1:8000` 而失败 | 读三个 env 文件 + 你的确认 |
| ✅ PM2 布局/解释器已对齐（原不一致） | 原 `ecosystem.config.cjs` 启动 `../langchainServer/agent_service/run.py` + conda 解释器；**已改为** `.venv/bin/python` + 根 `run.py`，三个路径常量均可用环境变量覆盖（§14.4） | 读 `ecosystem.config.cjs` + `node --check` + 按真实目录加载配置校验路径存在性 |
| ⚠️ 两套部署机制并存 | 仓库同时有 PM2（`ecosystem.config.cjs`）与 Docker（`Dockerfile` + `docker-compose.yml`，后者含 `postgres` 服务）；你已确认后续上线走 **PM2** | 文件树 + 你的确认 |
| 可选库缺失 | `cryptography`、`pytest`、`langchain-anthropic`、`langchain-google-genai` 未安装；`pydantic-settings`、`tiktoken`、`httpx`、`orjson` 已存在（传递依赖） | `importlib.util.find_spec` |

### 1.2 现有接口（必须保持兼容）

| 方法 | 路径 | 备注 |
| --- | --- | --- |
| POST | `/api/chat` | SSE：`start` → `delta`* → `done`，异常 `error` |
| DELETE | `/api/chat/{conversationId}`、`/api/history/{conversationId}` | 清空会话 |
| GET | `/api/history`、`/api/history/{conversationId}` | 分页 / 详情 |
| POST | `/api/history` | 建会话 |
| POST | `/api/knowledge/ingest`、`/api/knowledge/search`、GET `/api/knowledge/stats` | 文档 RAG |
| GET | `/`、`/health`、`/api/models` | 服务发现 / 健康 / 模型列表 |

### 1.3 已定位的问题（按严重度排序）

| # | 问题 | 位置 | 影响 |
| --- | --- | --- | --- |
| 1 | **无服务间鉴权**：`userId` 来自请求体/查询参数 | `main.py` 全部路由 | 可伪造身份读写他人会话；引入用户自有密钥后升级为密钥面风险 |
| 2 | `MILVUS_USER` / `MILVUS_PASSWORD` 在 `config.py` 中**不存在**，`vector_store` 单例也没传 | `config.py:57-60`、`vector_store.py:148-154` | `docker-compose.yml` 注入的用户名密码被静默忽略，只能走 token，认证方式与文档不符 |
| 3 | 默认模型名与端点真实 id 不一致 | `config.py:29,34` | 前端下拉默认项调用即失败 |
| 4 | 异常原文直接回传前端：`yield _sse("error", {"error": str(exc)})` | `main.py:172` | 上游报错体可能夹带密钥/内网地址，回显给前端即泄露 |
| 5 | 模型实例缓存无上限、只按模型名做 key | `agent.py:141-150` | 引入用户 base_url/key 后：内存无界增长、同模型名不同 key 串用 |
| 6 | `usage` 恒为 `null`，无 token 计量 | `main.py:167` | 无法回答「复用到底省了多少 token」——也就是需求①的验收指标缺失 |
| 7 | 历史消息全量入 prompt，无窗口/摘要 | `database.py:122-136` + `agent.py:115-132` | 长会话 token 成本线性上涨 |
| 8 | 客户端中断/异常时 assistant 消息不落库 | `main.py:149-172` | 历史记录缺一轮，且组件资产无法从历史回填 |
| 9 | 会话双写（OCRServer 的 MySQL + 本服务 PG），无真相源约定 | `services/chat.js:310-313` + `database.py` | 两边可能不一致，广场/Agent 依赖的历史以哪边为准未定义 |
| 10 | `.env.example` 中含**疑似真实**的 Milvus Token | `.env.example` | 模板文件进版本库即等于泄露，建议立即轮换并清空模板 |

---

## 2. 目标架构总览

```
                    ┌────────────────────────────── 前端 my-tsx-react-app ──────────────────────────────┐
                    │  聊天页（沿用 SSE）   模型设置页（自带 Key）   作品广场（新）   源码查看/复刻（新）      │
                    └───────────────────────────────┬───────────────────────────────────────────────────┘
                                                    │ JWT
                    ┌───────────────────────────────▼───────────────────────────────────────────────────┐
                    │  OCRServer（Node）  鉴权 / 积分 / MySQL 历史 / 视觉理解(MiMo) / 反向代理              │
                    └───────────────────────────────┬───────────────────────────────────────────────────┘
                                    内部调用 + 共享密钥签名（X-Internal-Token / HMAC）
                    ┌───────────────────────────────▼───────────────────────────────────────────────────┐
                    │  SnapUI Agent Service（本工程，重写后按 app/ 分包）                                  │
                    │                                                                                   │
                    │  api/        chat(SSE) · history · knowledge · components(广场) · model-profiles    │
                    │  services/   chat_service → 意图路由 → ┌ 直接复用（0 生成 token）                    │
                    │                                        ├ 参考生成（few-shot）                        │
                    │                                        ├ 全量生成（现状）                            │
                    │                                        └ 附件改造（引入源码二次修改）                 │
                    │  agent/      create_agent(LangGraph) + 工具：组件检索/取源码/知识检索/入库/分类          │
                    │  llm/        provider 工厂 + 实例 LRU/TTL 缓存 + 用户模型配置（加密/内联）              │
                    │  repositories/  MySQL(会话只读·组件·配置) · Milvus(索引) · 嵌入模型                   │
                    │  core/       配置 · 日志脱敏 · 内部鉴权 · SSRF 校验 · 统一错误                          │
                    └───────────┬───────────────────────────┬───────────────────────────┬─────────────────┘
                                │                           │                           │
                        MySQL（单库，表域分工）
                ┌───────────────┴────────────────┐            Milvus（检索索引）          用户自定义 LLM 端点
        [OCRServer 域]                    [本服务域]          design_knowledge(文档)      （受 SSRF 白名单约束）
        users / credits /            components / component_versions      ui_components(组件)
        conversations / messages /   component_likes / forkes / reports
        user_sign_ins                component_composition / component_usage
                                     conversation_turns / model_profiles
        ↑ 本服务只读（独立只读账号）    ↑ 本服务读写（禁止跨域写、禁止跨服务 JOIN）
```

**三条不变量（贯穿全部改动）**

1. **SSE 契约向后兼容**：只新增字段，不改/不删既有字段与事件名；`start`/`delta`/`done`/`error` 语义不变。
2. **消息是原始真相，组件表是派生索引**（§13.2）：`components` 等资产表可随时 `DROP` 并由 `scripts/backfill_components.py` 从 `messages` 回填；反向依赖（消息引用组件）**禁止**。
3. **密钥永不进日志、不进响应、不进 Milvus**：落库即密文，出站即校验，出错即脱敏。

---

## 3. 需求①：组件原子化与向量复用

目标：当多个用户生成过高度相似的按钮/表单/Navbar/侧边栏时，**不再调用大模型**，直接返回库内源码；相似但不完全相同的，用库内组件作范例来降低生成成本。

### 3.1 资产模型

组件按「粒度」和「类别」两个维度标注，二者都在入库时确定。

**粒度 `kind`**

| kind | 含义 | 是否对外复用 |
| --- | --- | --- |
| `page` | 一次生成的完整结果（含入口组件 `Component`） | 是（广场主体） |
| `composite` | 组合块（卡片、表单、表格等，由原子组成） | 是 |
| `atomic` | 原子件（按钮、输入框、IconButton…） | 是（需求①的主要命中对象） |

**类别 `category`**（封闭枚举，用于向量检索前过滤，避免跨类误命中）

```
layout:    navbar sidebar footer container grid tabs breadcrumb
form:      input textarea select checkbox radio switch date-picker form-group login-form
action:    button icon-button dropdown menu
display:   card table list badge avatar tag tooltip modal drawer toast accordion pagination progress skeleton chart image
feedback:  alert spinner empty-state
```

**原子性判定（零 token 规则，不额外调模型）**

- 从生成结果里按代码块解析出每个 `function Xxx(...)` 函数组件；
- 函数体 < 5 行且不含 props/状态 → 视为碎片，不单独入库（并入宿主组件）；
- 函数体内引用其他自定义组件，或包含根布局容器（`min-h-screen`、多列 grid）→ 标为 `composite` 或 `page`；
- 其余标为 `atomic`，再按关键词 + 类心向量归入 `category`（关键词优先，命中不了才用向量最近类心，两者都不额外耗生成 token）。

> 诚实说明：Python 侧没有 TSX 解析器。首版用「代码块正则 + 规范化文本指纹 + 关键词/向量分类」，不做真正的 AST 分析，因此对动态生成、高阶组件、`export default` 多形态的识别会有误差。可选增强是引入 `tree-sitter` + `tree-sitter-typescript`（新依赖，需网络）做真 AST；默认不启用，等首版数据说话。

### 3.2 存储设计

**原则**：源码与元数据只存 MySQL（C2 单库，§13）；Milvus 只存 `id + 向量 + 少量可过滤标量`，不存源码（避免 64KB varchar 限制、避免删除/更新 Milvus 的一致性麻烦，也避免把用户代码散落两处）。

**组件资产域新增表（迁移 `0001_components.sql`）**

> C2 定稿后落在**单一 MySQL**，字段与索引不变，方言差异（`UUID`→`CHAR(36)`、`TIMESTAMPTZ`→`DATETIME(3)`、`JSONB`→`JSON`、`TEXT[]`→`JSON`、时区统一 UTC）与建库要点见 **§13.6**。下表保留 PG 语法作为字段设计参照。

```sql
-- 组件主表（真相源）
CREATE TABLE IF NOT EXISTS components (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   VARCHAR(64)  NOT NULL,
    name            VARCHAR(120) NOT NULL DEFAULT '',
    kind            VARCHAR(16)  NOT NULL,            -- page | composite | atomic
    category        VARCHAR(32)  NOT NULL DEFAULT 'other',
    description     TEXT         NOT NULL DEFAULT '',
    language        VARCHAR(16)  NOT NULL DEFAULT 'tsx',
    code            TEXT         NOT NULL,            -- 原始源码
    entry_wrapper   VARCHAR(32)  NOT NULL DEFAULT 'component', -- 回传前端时的包装方式
    digest          CHAR(64)     NOT NULL,            -- 规范化指纹（sha256）
    semantic_text   VARCHAR(2048) NOT NULL DEFAULT '',-- 被嵌入的检索串（与查询侧同规则构造）
    embedding_model VARCHAR(64)  NOT NULL DEFAULT '',
    embedding_dim   INTEGER      NOT NULL DEFAULT 1024,
    visibility      VARCHAR(16)  NOT NULL DEFAULT 'private',  -- private|unlisted|public
    status          VARCHAR(16)  NOT NULL DEFAULT 'draft',    -- draft|published|removed|rejected
    quality_score   REAL         NOT NULL DEFAULT 0,
    reuse_count     INTEGER      NOT NULL DEFAULT 0,   -- 被复用（直接返回）次数
    ref_count       INTEGER      NOT NULL DEFAULT 0,   -- 被当作参考次数
    like_count      INTEGER      NOT NULL DEFAULT 0,
    fork_count      INTEGER      NOT NULL DEFAULT 0,
    fork_of         UUID         REFERENCES components(id) ON DELETE SET NULL,
    source_conversation_id VARCHAR(64),
    source_message_id      BIGINT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_components_owner_digest ON components(owner_user_id, digest);
CREATE INDEX IF NOT EXISTS idx_components_owner      ON components(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_components_gallery    ON components(visibility, status, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_components_category   ON components(category);

-- 组合关系：谁由哪些原子组成（支撑"改这一个按钮，影响的页面有哪些"）
CREATE TABLE IF NOT EXISTS component_composition (
    parent_id  UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    child_id   UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    slot       VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY (parent_id, child_id, slot)
);

-- 版本链（迭代不覆盖历史；广场默认展示最新 published 版本）
CREATE TABLE IF NOT EXISTS component_versions (
    id           BIGSERIAL PRIMARY KEY,
    component_id UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    code         TEXT NOT NULL,
    digest       CHAR(64) NOT NULL,
    changelog    VARCHAR(500) NOT NULL DEFAULT '',
    created_by   VARCHAR(64) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (component_id, version)
);

-- 每次复用/参考/生成都记账，用于 ①成本报表 ②热度排序
CREATE TABLE IF NOT EXISTS component_usage (
    id             BIGSERIAL PRIMARY KEY,
    component_id   UUID REFERENCES components(id) ON DELETE SET NULL,
    conversation_id VARCHAR(64),
    user_id        VARCHAR(64),
    mode           VARCHAR(16) NOT NULL,   -- reuse | reference | fresh | adjust
    similarity     REAL,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    est_saved_tokens INTEGER NOT NULL DEFAULT 0,
    model          VARCHAR(128),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_component_usage_time ON component_usage(created_at DESC);
```

同时给 `messages` 加两列（幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`）：`attachments JSONB`、`tool_trace JSONB`。

**Milvus 新集合 `ui_components`（schema 模式，非 quick 模式）**

现有 `design_knowledge` 是 quick 模式建的（字段固定为 `id/vector/text/source`），组件库需要标量过滤，所以必须用 schema 模式新建，**不动老集合**：

```python
schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
schema.add_field("id",            DataType.VARCHAR, is_primary=True, max_length=64)
schema.add_field("vector",        DataType.FLOAT_VECTOR, dim=1024)      # 复用现有嵌入模型
schema.add_field("kind",          DataType.VARCHAR, max_length=16)
schema.add_field("category",      DataType.VARCHAR, max_length=32)
schema.add_field("visibility",    DataType.VARCHAR, max_length=16)
schema.add_field("status",        DataType.VARCHAR, max_length=16)
schema.add_field("owner_user_id", DataType.VARCHAR, max_length=64)
schema.add_field("semantic_text", DataType.VARCHAR, max_length=2048)

index = client.prepare_index_params()
index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
# 标量索引用于加速 category/status/visibility 过滤；若托管实例不支持则跳过（过滤仍可用，只是慢）
index.add_index(field_name="category", index_type="INVERTED")

client.create_collection(collection_name="ui_components", schema=schema, index_params=index)
```

- 检索过滤表达式：`category in ["action","form"] and status == "published" and visibility in ["public","unlisted"]`
- `semantic_text` 回填用于人工核对命中原因（排障利器）；集合可随时由 MySQL 资产表重建。
- **待现场验证**：Zilliz Serverless 是否支持 `INVERTED` 标量索引 / 单集合字段配额。当前该实例从本机 DNS 不通，无法实测，留给 P1 的第一步在生产服务器上验证（见 8 节验收项）。

### 3.3 检索串构造（查询侧与入库侧必须同一函数）

命中率的上限由「被嵌入的文本」决定，而不是由向量库决定。统一由 `services/component_text.py::build_semantic_text()` 生成两侧文本：

```
入库（组件侧）：  "{kind}/{category} | {name} | {description} | 属性: {attrs} | 风格: {style}"
查询（需求侧）：  "{category_hint}/{kind_hint} | {归一化需求} | 属性: {attrs} | 风格: {style}"
```

- **归一化**：剥离口语前缀（"帮我/请/给我/生成一个/做一个/来一个"）、去尾部标点、压缩空白、全半角统一、去重复词。
- **属性抽取**（正则词表，零 token）：颜色（红/蓝/绿/主色/深色/浅色/渐变）、尺寸（大/中/小/full-width）、形状（圆角/直角/胶囊/方形）、状态（hover/禁用/加载/选中）、布局（固定顶部/侧边/两栏/居中）。
- **风格 token**：极简/拟物/玻璃拟态/暗色/高对比/扁平。
- 两侧规则写在一个模块、被两个入口调用（入库与检索），并在文档与代码注释里互相指向——这是「一处改动必须同时生效」的关键点。

### 3.4 三档策略与判定流程

```
POST /api/chat
  1) 意图路由（规则优先，零 token）
       附件含源码 或 命中「改成/调整/换成/加一个/去掉/把…改成…」且最近一条 AI 消息含代码块
         → ADJUST
       否则 → CREATE
  2) CREATE 分支
       q = build_semantic_text(需求)
       hits = milvus.search(q, filter=..., limit=8)        # 一次嵌入 ≈ 20 token
       top = hits[0]
       if top.score >= T_REUSE and structure_ok(top) and specificity_ok(需求):
             → REUSE      不调用生成模型，直接返回 top.code
       elif top.score >= T_REF:
             → REFERENCE  取 top1..3 源码 + 简短说明注入提示词，要求"改造为满足需求的变体"
       else:
             → FRESH      现状全量生成
  3) 生成/复用结束后统一：抽取子组件 → 指纹去重 → 入库 → 记 component_usage
```

**`structure_ok(candidate)`（防误复用的硬门槛）**

1. `category` 与查询侧类别一致（向量前已过滤，双保险）；
2. 需求中抽取到的属性与候选的 `semantic_text` 属性**无冲突**（如需求"红色"，候选是"蓝色" → 拒绝）；
3. 候选代码通过 4.5 的静态合规校验；
4. 候选 `status == 'published'` 且未被下架。

**`specificity_ok(需求)`（数据驱动的新增门槛，很重要）**

实测发现"生成一个按钮"这类**无约束需求**与任何具体按钮的相似度只有 0.58，但与另一句同义的口语化需求高达 0.89。也就是说：无约束需求会命中「某个具体样式」的组件，用户拿到的可能不是想要的变体。因此：需求中**至少含 1 个属性或结构约束**才允许直接复用；否则降级为 REFERENCE 档（用库内组件当范例生成），或由前端提供"换一个"候选列表。

### 3.5 阈值依据（用你自己的嵌入模型实测，非拍脑袋）

实测数据（`qwen3.7-text-embedding`，1024 维，COSINE）：

| 样本对 | 实测相似度 | 应有判定 |
| --- | --- | --- |
| 按钮A「蓝色主色圆角按钮带 hover」↔ 按钮B「按钮组件，圆角、蓝色背景、悬停变化」 | **0.9123** | 可复用 |
| 按钮A ↔ 按钮C「A rounded blue button with a hover state」（跨语言同义） | **0.9066** | 可复用 |
| 导航A「顶部导航栏含 logo 和搜索框」↔ 导航B「Navbar，左 logo 右搜索框，固定顶部」 | **0.8511** | 参考生成 |
| 「生成一个按钮」↔「帮我生成一个按钮组件」（同义极简） | **0.8942** | 边界（受 specificity 门槛压制） |
| 蓝圆角按钮 ↔ 红方形按钮（同类、属性冲突） | **0.5544** | 不可复用 |
| 按钮A ↔ 导航A（跨类） | 0.3866 | 不可复用 |
| 按钮/导航 ↔ 登录表单（跨类） | 0.42 ~ 0.52 | 不可复用 |
| 「生成一个按钮」↔「蓝色圆角按钮带 hover」 | 0.5775 | 不可复用 |

**由此得到的初值**：`T_REUSE = 0.90`、`T_REF = 0.80`、入库判重 `T_DUP = 0.97`。

区分度看起来足够（语义等价 0.85–0.92 vs 跨类 0.39–0.52 vs 属性冲突 0.55），但**样本量太小（本次 10 句）不足以定稿**。因此 P1 必须做 `scripts/calibrate_thresholds.py`：从会话历史（`messages`）里取 ≥100 对带标注样本（同义/近似/冲突/无关四类），扫阈值画 precision-recall，把 T_REUSE 定在「误复用率 ≤ 2%」对应的点上，并记录到文档。首版阈值取保守值，宁可少命中也不能返回错组件。

### 3.6 入库、去重与版本

1. **抽取**：对生成结果（或 ADJUST 结果）按代码块解析出候选组件；
2. **规范化指纹**：去注释/去空白/统一引号/属性顺序排序 → `sha256`；
3. **精确去重**：`(owner_user_id, digest)` 唯一索引命中 → 不新增，`reuse_count += 1`，记录 `component_usage`；
4. **近似去重**：向量 top1 ≥ `T_DUP` → 视为同一组件的不同版本 → 走 `component_versions` 新版本，而非新记录；跨用户近似 → 标记 `fork_of`；
5. **可见性默认值**：用户对话产物默认 `visibility='private', status='draft'`；**只有用户显式"发布"才变 public**（隐私与合规底线，不能默认公开）；
6. **质量分**：`quality_score = f(静态校验通过, 复用次数, 点赞 - 举报, 是否含无障碍属性)`，用于广场排序与复用优先级（复用优先选 `quality_score` 高者）。

### 3.7 冷启动：用历史数据回填 + 一致性重建

- `scripts/backfill_components.py`：遍历 assistant 消息 → 抽取组件 → 指纹/向量 → 入库（幂等，可重复跑）。这是**现成语料**，能让复用率在灰度当天就有意义，而不是等新用户慢慢攒。消息来源取决于 §12 的决策（C1 经 OCRServer 内部接口导出，A′ 读本服务缓存表）。
- `scripts/reindex_components.py`：以 MySQL 组件表为准全量重建 Milvus 集合（换嵌入模型、集合损坏、迁移实例时使用）。

### 3.8 计量与「省了多少 token」的可验证口径

现状 `usage` 恒为 `null`（问题 #6），导致需求①无法验收。改造后：

- 生成侧开启流式用量统计（`stream_usage`），拿不到 usage 的端点用已安装的 `tiktoken` 估算；
- 每次对话写一条 `component_usage`：`mode`、`similarity`、`prompt_tokens`、`output_tokens`、`est_saved_tokens`；
- `est_saved_tokens` 口径写明：`reuse` 记「若走 REFERENCE 档的基线输出 token 均值」；`reference` 记「FRESH 档基线均值 − 实际输出」。基线均值来自开关关闭期间的历史统计（用 `GENERATION_POLICY=legacy` 采集）。
- 新增 `GET /api/stats/generation?days=7` 返回：各档占比、平均输入/输出 token、估算节省、复用命中率、误复用回退率（前端"换一个"按钮的点击率是唯一可信的误复用信号，必须埋点）。

### 3.9 复用结果如何表达给前端（跨工程约束，必须提前对齐）

实测确认 OCRServer 的 SSE 解析器（`services/chat.js:360-400`）**只认 `data:` 行里的 `text` / `error` / `stopReason` 三个字段，忽略 `event:` 名**，并且它是重新编码后转发给前端的。结论：

- Python 侧在 `done` 事件里新增 `mode`、`componentId`、`similarity`、`reused` 字段是**安全的**（旧字段不变、不破坏解析）；
- 但**前端要真正看到这些字段，OCRServer 必须增加白名单透传**（4.6 列出）。在 OCRServer 未升级前，功能可用、标记不可见——不影响主流程。
- 复用返回的代码必须仍满足前端契约：函数名 `Component`、无 `export`、无 `BrowserRouter`、跳转用 `Link/useNavigate`（前端 `chatApi`/`useChat` 的沙箱约定）。因此入库时额外记录 `entry_wrapper`：若命中的是 `atomic`，返回前按模板包装成合法的 `Component`。

### 3.10 风险与回退

| 风险 | 控制手段 |
| --- | --- |
| 误复用返回不合适组件 | 三重门槛（阈值 + 属性冲突检测 + specificity）+ 前端"换一个/仍要 AI 生成"按钮 + `GENERATION_POLICY` 开关一键回退 legacy |
| 命中率低导致白做 | 回填历史语料 + 计量报表；把"重复需求占比"作为产品指标暴露出来 |
| 组件库被灌水/抄袭 | 发布需显式动作 + 静态校验 + 向量查重 + 举报入口（4.5） |
| 向量库不可用 | RAG 与复用检索失败一律降级为 FRESH 生成（现有代码已是这个策略，保留并显式记录 mode=fresh, degraded=true） |
| 换嵌入模型导致全库失效 | 集合记录 `embedding_model/dim`，维度或模型变更时强制走 `reindex_components.py`，不允许混用 |

---

## 4. 需求②：进阶为 AI Agent 工程 + 作品广场

### 4.1 Agent 运行时

已验证 `langchain 1.3.18` 提供 `langchain.agents.create_agent`（基于已安装的 `langgraph 1.2.11`），签名包含 `model / tools / system_prompt / middleware / checkpointer / store` 等。方案采用它，而不是手写循环：工具调用、循环终止、流式事件、后续 human-in-the-loop 都由框架承担，代码量更小、边界更清楚。

```python
# app/agent/graph.py（示意）
agent = create_agent(
    model=chat_model,                 # 每请求由 llm/factory 按用户模型配置构造
    tools=[search_component_library, get_component_source, search_knowledge,
           save_component, classify_component],
    system_prompt=SYSTEM_PROMPT,      # 含前端沙箱路由契约（Link/useNavigate，禁 window.location）
    checkpointer=None,                # 见下方"会话状态"决策
    middleware=[],                    # 预留：摘要/裁剪/工具审计
)
```

**会话状态**：本方案**不引入 PG checkpointer**（`langgraph-checkpoint-postgres` 未安装，且会引入第三处会话状态）。保持「每轮读历史 → 组装 messages → 调 agent → 写回」的无状态模式；**历史的来源按 §13.1**（只读账号直连 `conversations`/`messages`）。代价：每轮重建上下文、没法用 LangGraph 的断点续跑与人工介入；收益：会话真相源只有一处、改动小、可随时加。若后续要做"生成中断后可继续/人工审批发布"，再升级为 PG checkpointer（已在依赖清单里标为可选）。

**能力降级（与需求③强耦合，必须首版就做）**：用户自带模型不一定支持 function calling。因此 `agent_mode` 分三档：

| 档 | 行为 | 触发 |
| --- | --- | --- |
| `tools` | 走 create_agent 工具循环 | 模型自测支持 tools |
| `pipeline` | 走现有单轮流水线（检索 + 提示词 + 生成） | 模型不支持 tools，或 tools 调用连续失败 2 次 |
| `off` | 纯生成（不检索不工具），排障用 | 配置开关 |

`supports_tools` 探测结果按 `(base_url, model)` 缓存，避免每次请求都试错。

### 4.2 工具清单（每个工具 = 一个原子能力）

| 工具 | 入参 | 出参 | 用途 |
| --- | --- | --- | --- |
| `search_component_library` | `query, category?, kind?, top_k?` | 候选 `[{id,name,category,similarity,semantic_text}]`（不含源码，省 token） | Agent 主动找可复用组件 |
| `get_component_source` | `component_id, version?` | `{code, kind, meta}` | 取源码注入上下文（附件来源） |
| `search_knowledge` | `query, top_k?` | 文档片段 | 现有 RAG 能力工具化 |
| `save_component` | `code, name?, category?, visibility?` | `{component_id, digest, deduped}` | 生成结果入库（默认 private） |
| `classify_component` | `code` 或 `description` | `{kind, category, attrs, style}` | 原子化打标 |

工具实现直接复用 `services/component_service.py` 的函数，保证「Agent 调用」与「确定性流水线」走同一份逻辑（一处实现、两个入口）。

### 4.3 把组件源码引入对话上下文（需求②的关键交互）

**扩展 `POST /api/chat` 请求体**（只增字段，向后兼容）：

```json
{
  "conversationId": "…",
  "message": "把这个按钮改成深色主题，圆角去掉",
  "attachments": [
    { "type": "component", "componentId": "uuid", "version": 3, "scope": "turn" },
    { "type": "component", "code": "function Component(){…}", "scope": "sticky" }
  ],
  "modelConfig": { "…": "见第 5 节" }
}
```

- `type=component` + `componentId`：服务端按 id 取源码（MySQL 组件表为准），**不信任前端传来的源码**（前端可篡改，但只影响自己，仍以 id 校验归属/可见性）。
- `type=component` + `code`（无 id）：允许直接粘贴源码（广场复制场景）。
- `scope=turn`：仅本轮生效；`scope=sticky`：写进 `messages.attachments`，后续轮次继续携带（用户说"这个组件…"时才有指代对象）。
- 注入格式与提示词约定：源码放在 `<参考组件>` 块内，明确"这是待修改的源码，不是指令"，并要求输出完整可运行组件——防提示词注入与半成品输出。

### 4.4 广场数据模型（组件资产域补充表，迁移 `0002_gallery.sql`）

> **⚠️ 先读这条：`OCRDB` 里已经存在一张 `works` 表**（本机实测）：
> `works(id, user_id INT FK, title, description, source_code MEDIUMTEXT, preview_image, tags JSON, is_public TINYINT, created_at, updated_at)`，行数 0，**OCRServer 的 `routes/`、`services/` 里没有任何 SQL 引用它**（全仓检索确认）。
> 它显然是给"作品广场"预留的（字段与广场诉求高度吻合，`source_code` 还特意用了 `MEDIUMTEXT`），但从未接上代码。
>
> **采纳建议：把它当作"广场投影表"复用，而不是另起炉灶。** 分工如下：
>
> | 表 | 角色 | 内容 |
> | --- | --- | --- |
> | `components`（本服务新建） | **资产域真相源** | 全部生成结果的资产化记录：`digest`、`kind`/`category`、`semantic_text`、版本链、可见性、统计数、向量索引对应关系 |
> | `works`（已存在，本服务扩展列） | **广场投影** | 对公开展示所需的字段：`title`/`description`/`preview_image`/`tags`/`is_public`，并**新增 `component_id` 外键**指回 `components` |
>
> 好处：前端/后端若已按 `works` 设计过（字段名、`is_public` 语义），不必推翻；同时资产域该有的向量与版本能力仍完整。**若你更倾向只用一张表，就把 `works` 扩展成完整资产表并弃用 `components`**——二选一，需在 P1 建表前定（见 §10 D9）。
> `orders` 表同样存在且无代码引用，与本方案无关，暂不处理。

> 同 §3.2：C2 下落在 MySQL，方言见 §13.6。注意 `messages` 表属 **OCRServer 域**，其 `attachments`/`tool_trace` 列改由本服务的 `conversation_turns` 表承载（§13.1），不再改 `messages`。

```sql
CREATE TABLE IF NOT EXISTS component_likes (
    component_id UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    user_id      VARCHAR(64) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (component_id, user_id)
);

CREATE TABLE IF NOT EXISTS component_forks (
    id                  BIGSERIAL PRIMARY KEY,
    source_component_id UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    new_component_id    UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    user_id             VARCHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS component_reports (
    id                 BIGSERIAL PRIMARY KEY,
    component_id       UUID NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    reporter_user_id   VARCHAR(64) NOT NULL,
    reason             VARCHAR(500) NOT NULL DEFAULT '',
    status             VARCHAR(16)  NOT NULL DEFAULT 'pending',  -- pending|accepted|rejected
    handled_by         VARCHAR(64),
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    handled_at         TIMESTAMPTZ
);

-- 回合级元数据：附件、工具调用轨迹、生成模式、token 用量
-- C2 定稿：不改 OCRServer 的 messages 表，改由本服务自己的表承载（§13.1）
CREATE TABLE IF NOT EXISTS conversation_turns (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL,   -- 逻辑关联 OCRServer 的 conversations，不建跨服务外键
    turn_index      INTEGER     NOT NULL,   -- 第几轮（从 1 起）
    user_id         VARCHAR(64) NOT NULL,
    attachments     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    tool_trace      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    generation_mode VARCHAR(16) NOT NULL DEFAULT 'fresh',  -- reuse|reference|fresh|adjust
    model           VARCHAR(128),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, turn_index)
);
```

### 4.5 发布合规与静态校验（用户作品公开可见 ⇒ 必须有）

`services/moderation.py`，**纯静态、零 token、可单测**：

| 检查 | 规则 | 处置 |
| --- | --- | --- |
| 编译性 | 含 `function Component`（或 entry_wrapper 可包装）、括号/标签配平 | 拒绝发布 |
| 外联 | `fetch(`/`XMLHttpRequest`/`WebSocket`/`<img src="http`/`import(` 动态导入 | 拒绝（沙箱内也无意义，且是数据外泄面） |
| 浏览器越权 | `window.location`、`document.cookie`、`localStorage`、`eval`、`new Function`、`dangerouslySetInnerHTML` | 拒绝或降级为 private |
| 依赖 | `import` 白名单（仅 `react`、`react-router-dom` 及沙箱已提供者） | 拒绝 |
| 查重 | 向量 top1 ≥ `T_DUP` 且属他人 | 提示"疑似重复"并转人工/打回 |
| 敏感词 | 词表（走配置，不硬编码在代码里） | 拒绝并记录 |

> 注意：这套规则必须与前端沙箱（`SandboxRouter`/`LivePreview` + `babel/standalone`）的实际能力/CSP **对齐后才有效**。需要前端工程确认沙箱注入的全局对象白名单；建议由前端在预览时同样做一次运行期隔离（iframe + CSP），服务端静态校验只是第一道闸。

### 4.6 广场 API 契约与跨工程对接清单

**Python 新增接口**（统一 `/api/components`，均需内部鉴权 + 用户身份）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/components` | 保存（默认 private draft）：`{code,name,kind,category,description,visibility,sourceConversationId}` |
| GET | `/api/components` | 广场列表：`?page&pageSize&category&kind&sort=hot|new&q=`（仅 `visibility=public & status=published`） |
| GET | `/api/components/{id}` | 详情（含源码、版本列表、作者归属、是否已点赞） |
| GET | `/api/components/{id}/similar` | 相似推荐（复用向量库） |
| PATCH | `/api/components/{id}` | 改名/描述/可见性（仅作者） |
| DELETE | `/api/components/{id}` | 软删（`status=removed`，仅作者） |
| POST | `/api/components/{id}/publish` | 显式发布（过 4.5 校验） |
| POST | `/api/components/{id}/unpublish` | 下架 |
| POST | `/api/components/{id}/versions` | 迭代新版本（含 changelog） |
| POST | `/api/components/{id}/fork` | 复刻到自己名下，返回新 id + 源码 |
| POST/DELETE | `/api/components/{id}/like` | 点赞 / 取消 |
| POST | `/api/components/{id}/report` | 举报 |
| GET | `/api/users/me/components` | 我的作品 |
| GET | `/api/stats/generation` | 生成/复用成本报表（3.8） |

**必须由 OCRServer 配合的改动（本仓库之外）**

1. 新增转发路由 `/api/components/*`、`/api/model-profiles/*`，透传用户 JWT 身份；
2. 调用 Python 时带共享密钥与用户身份（见 5.3 的 `X-Internal-Token` + `X-User-Id`）；
3. `/api/chat` 的 SSE 解析器增加**白名单透传**：把 `done` 事件里的 `mode/componentId/similarity/reused` 一并转发（现状只透 `text/error/stopReason`）；
4. 透传 `attachments`、`modelConfig` 请求字段；
5. 积分规则调整（自有 Key 是否扣积分由产品决定）。

**必须由前端工程配合的改动（本仓库之外）**

1. 作品广场页（列表/筛选/详情/源码复制/一键使用）；
2. 「引入对话」按钮：把广场组件以 `attachments` 形式带进聊天；
3. 模型设置页：APIKey/BaseURL/ModelName + 连通性测试；
4. 复用结果提示 + 「换一个 / 仍用 AI 生成」按钮（这是误复用率的唯一可信埋点）；
5. 沙箱运行期隔离（iframe + CSP），与服务端静态校验对齐。

---

## 5. 需求③：用户自定义 APIKey / BaseURL / ModelName

### 5.1 两条落地路径（A 必做，B 作为可选开关，默认关闭）

| 路径 | 说明 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A. 内联传参** | 前端从本地存储读 Key，随每次 `/api/chat` 的 `modelConfig` 上传；服务端**不落库**、不写日志，用完即忘 | 无密钥存储责任，最快落地，最安全 | 用户每次换设备要重填；前端要把 Key 存 localStorage |
| **B. 配置档（加密落库）** | `POST /api/model-profiles` 保存，服务端用对称加密存密文；请求里用 `modelProfileId` 引用 | 体验好，可多套配置、可测连通性 | 需要 `cryptography`（当前未安装）与主密钥管理 |

首版建议：**A 必做，B 作为可选开关**（`MODEL_PROFILE_STORAGE=disabled|encrypted`），默认 `disabled`，避免在没有主密钥管理的情况下先背上密钥托管责任。

**优先级**：请求内联 `modelConfig` > `modelProfileId` > 服务端默认（`.env`）。缺省时完全等价于现状 → OCRServer 不改也能继续跑，保证兼容。

### 5.2 加密与脱敏

- 加密：`cryptography` 的 `Fernet`（AES-128-CBC + HMAC），主密钥来自 env `SECRET_ENCRYPTION_KEY`（base64 32 字节，**不入镜像、不进版本库**）；缺失时 `MODEL_PROFILE_STORAGE` 强制降级为 `disabled` 并在启动日志告警。
- 存储表：`model_profiles(id, user_id, label, provider, base_url, model_name, api_key_cipher, key_last4, extra jsonb, is_default, created_at, updated_at)`，同一用户 `label` 唯一。
- 读取只回掩码：`apiKeyMasked = "sk-****" + key_last4`，**任何接口都不返回明文**。
- 日志脱敏：`core/logging.py` 装 `logging.Filter`，对 message 与 `exc_info` 做正则替换（`sk-[A-Za-z0-9_\-]{8,}`、`Bearer\s+\S+`、`(?i)(api[_-]?key|token|authorization|password)\s*[=:]\s*\S+`）。`main.py:172` 现在直接把 `str(exc)` 回传前端——改为返回**错误码 + 安全描述**，详情只进服务端日志（脱敏后）。
- 禁止项：Key 不得进入 Milvus、不得进入 `component_usage`、不得进入 SSE、不得进入 `tool_trace` 落库。

### 5.3 SSRF 与出站安全（用户可控 BaseURL 是本需求最大的风险）

用户能指定 `baseUrl` 就等于拿到一个"让服务器替我发请求"的能力。必须做的校验（`core/security.py::validate_outbound_base_url`）：

1. **协议**：仅 `https`（允许 `http` 需显式 `ALLOW_INSECURE_MODEL_URL=true`，默认 false）；
2. **主机**：解析 A/AAAA 记录 → 每个 IP 用 `ipaddress` 校验，拒绝 `is_private / is_loopback / is_link_local / is_reserved / is_multicast / is_unspecified`，并覆盖 IPv4-mapped IPv6（`::ffff:127.0.0.1` 这类绕过）；
3. **DNS rebinding**：校验通过后把解析结果**固定**到连接层（httpx transport 指定 IP，或短 TTL 缓存 + 二次校验），禁止"校验一次、用域名再连一次"；
4. **重定向**：`follow_redirects=False`（重定向是绕过校验的经典手法）；
5. **白名单模式**：`ALLOWED_MODEL_HOSTS`（逗号分隔，支持 `*.example.com`）；生产建议开启，企业自建网关场景尤其需要；
6. **额度与超时**：`connect=5s / read=60s / 总 300s`，单用户并发上限、每分钟请求上限（防被当作内网扫描器/代理滥用）；
7. **错误信息**：出站失败只回"模型端点不可达（已校验地址）"，不回显内网响应体。

### 5.4 模型工厂与实例缓存

```python
# app/llm/factory.py（示意）
def build_chat_model(cfg: ResolvedModelConfig):
    if cfg.provider == "openai-compatible":          # 覆盖绝大多数中转/自建端点
        return ChatOpenAI(model=cfg.model_name, api_key=cfg.api_key,
                          base_url=cfg.base_url, timeout=cfg.timeout,
                          max_retries=1, stream_usage=True)
    if cfg.provider == "deepseek":
        return ChatDeepSeek(model=cfg.model_name, api_key=cfg.api_key,
                            api_base=cfg.base_url, stream_usage=True)
    # anthropic / google 需新增依赖，默认不启用
    raise UnsupportedProvider(cfg.provider)
```

- **缓存 key**：`sha256(provider|base_url|model_name|sha256(api_key))` —— 用哈希而非明文；缓存为 LRU + TTL（默认 30 分钟）+ 上限（默认 64 条）。替代现状 `agent.py:141-150` 的无界 `dict`。
- 现状 `init_chat_model(model=..., model_provider="deepseek")` 只能读进程级 env 密钥，无法承载用户自定义端点，必须换成工厂 + 显式传参。
- `provider` 枚举先做 `openai-compatible` 与 `deepseek` 两类（覆盖主流中转/自建/官方），`anthropic`/`google` 作为可选扩展位。
- 修正 1.1 中发现的默认模型名错误：`DEEPSEEK_MODEL` 默认值改为端点真实 id（`deepseek-flash`），`AVAILABLE_MODELS` 同步修正。

### 5.5 能力与协议差异（必须显式降级，不能想当然）

| 差异 | 影响 | 处理 |
| --- | --- | --- |
| 不支持 function calling | Agent 工具链失效 | 4.1 的 `tools → pipeline` 降级，探测结果缓存 |
| 不支持流式 | SSE 无增量 | 一次性结果按 ~64 字符分片推 `delta`，保持前端体验一致（前端只认 `text`） |
| 不返回 usage | 无法计量 | 用已安装的 `tiktoken` 估算并标记 `estimate=true` |
| 参数不兼容（`temperature`、`max_tokens` 名称差异） | 请求 400 | 工厂内按 provider 映射；用户可传 `extraBody` 逃生口 |
| 响应格式非标准 | 解析失败 | 统一异常 → `error` 事件 `type=model_error`，绝不回显上游原文（含密钥） |

### 5.6 模型配置 API 契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/model-profiles` | 列出当前用户配置（**只回掩码**），并标注哪条是默认 |
| POST | `/api/model-profiles` | 新建：`{label,provider,baseUrl,modelName,apiKey,extra?}`（先过 SSRF 校验） |
| PUT | `/api/model-profiles/{id}` | 更新（不传 apiKey 表示保持不变） |
| DELETE | `/api/model-profiles/{id}` | 删除 |
| POST | `/api/model-profiles/{id}/test` | 连通性测试 + 返回该端点可见模型列表（供前端下拉） |
| POST | `/api/model-profiles/test` | 未保存前测试（内联参数，不落库） |
| GET | `/api/models` | 保留（平台预置模型），前端"平台模型 / 我的模型"两个 tab |

`/api/chat` 请求扩展（与 4.3 合并）：

```json
{
  "modelConfig": {
    "provider": "openai-compatible",
    "baseUrl": "https://…/v1",
    "modelName": "…",
    "apiKey": "sk-…",
    "timeoutSeconds": 60,
    "extraBody": {}
  },
  "modelProfileId": "可选，与 modelConfig 二选一，modelConfig 优先"
}
```

---

## 6. 目标目录结构（重写后的分包）

> **包名定为 `agent_service/`，不是 `app/`**：OCRServer 的 PM2 模板原本就期望 `../langchainServer/agent_service/run.py`，沿用这个名字可以让部署侧零改动。当前 PM2 配置的入口是根 `run.py`（与现有仓库结构一致）；重构后既可继续走根目录兼容壳（`from agent_service.run import main; main()`，`uv run python run.py` 照旧可用），也可用 `PYTHON_ENTRY=agent_service/run.py` 一行切到新入口。

```
agent_service/
  run.py                      # 真实启动入口（PM2 指向这里）
  main.py                     # FastAPI 装配：lifespan / 路由挂载 / 中间件 / 异常处理
  core/
    config.py                 # 配置（pydantic-settings，分环境；密钥仅来自 env）
    logging.py                # 日志 + 密钥脱敏过滤器
    security.py               # 内部鉴权(HMAC) · Fernet 加解密 · SSRF 校验
    errors.py                 # 统一异常 → SSE error / HTTP 状态映射
    deps.py                   # 依赖注入：当前用户、模型配置、仓储
  api/
    routers/chat.py history.py knowledge.py components.py model_profiles.py stats.py health.py
    sse.py                    # SSE 帧编码 + 事件协议版本常量
  schemas/                    # pydantic 请求/响应（按域拆：chat/component/model_profile/…）
  services/
    chat_service.py           # 一轮对话编排：鉴权→历史→路由→检索→生成/复用→落库→计量
    generation.py             # 三档策略实现（reuse/reference/fresh/adjust）+ 阈值读取
    component_service.py      # 抽取·指纹·分类·去重·版本·发布
    component_text.py         # build_semantic_text（入库与检索共用，单点）
    gallery_service.py        # 列表/详情/点赞/fork/举报/排序
    moderation.py             # 静态合规校验（可单测，零 token）
    usage.py                  # token 计量与节省估算
  agent/
    graph.py                  # create_agent 装配 + 能力降级
    tools.py                  # 5 个工具
    prompts.py                # 系统提示词（含前端沙箱路由契约）
    llm/factory.py cache.py   # provider 工厂 + LRU/TTL 实例缓存
  repositories/
    mysql.py                  # MySQL 连接池（asyncmy）+ 迁移执行器（schema_migrations）
    conversations.py messages.py components.py model_profiles.py
    vector_store.py           # Milvus：AsyncMilvusClient + schema 建集合 + 检索/写入
  migrations/0001_components.sql 0002_gallery.sql 0003_model_profiles.sql
scripts/
  backfill_components.py      # 历史消息 → 组件库
  reindex_components.py       # MySQL → Milvus 全量重建
  calibrate_thresholds.py     # 阈值校准（precision/recall）
  e2e_smoke.py                # 端到端冒烟（含 SSE 契约断言）
tests/                        # pytest：SSE 契约 / 检索复用 / 鉴权 / SSRF / 脱敏
run.py                        # 兼容壳：转调 agent_service/run.py（现有文档与本地习惯不变）
Dockerfile docker-compose.yml pyproject.toml uv.lock README.md AGENT.md CLAUDE.md
```

**为什么重写而不是就地小改**：需求②③引入的是横向能力（鉴权、密钥、Agent、广场），继续把逻辑堆在 `main.py`（现 259 行）与 `agent.py`（167 行）里，会让"生成路径"同时承担鉴权、检索、复用判定、计量、工具调用、密钥管理六件事，无法单测也无法回退。分包是这三条需求能否稳定落地的前提。

**保留不动的部分**：两种启动方式（`python run.py` 与 PM2 的 `agent_service/run.py`）、`/` 与 `/health` 的服务发现语义、SSE 事件名与字段、现有 8 组接口路径与参数、`design_knowledge` 集合。**会话表的归属按 §13 定稿执行（C2）**：删除本服务的 `conversations`/`messages` 表，历史改为只读账号直连（本服务永不写会话域）；MySQL 侧表结构完全不动。

---

## 7. 迁移与兼容策略

| 维度 | 策略 |
| --- | --- |
| 接口 | 现有路径/参数/响应 100% 保留；新能力只加字段、加新路径（`/api/components/*`、`/api/model-profiles/*`、`/api/stats/*`） |
| SSE | 事件名与 `start/delta/done/error` 结构不变；`done` 只新增可选字段 |
| 数据库 | **单一 MySQL（C2，§13）**，表域分工；迁移器：`schema_migrations(version, applied_at)` + 顺序执行 `migrations/*.sql`；全部语句幂等（`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`）；**只增不删**，回滚 = 回退镜像 + 关开关；本服务只对自有域表有 DDL 权限 |
| 向量库 | 老集合 `design_knowledge` 不动；新集合 `ui_components` 单独建；提供重建脚本 |
| 行为开关 | `GENERATION_POLICY=legacy|reuse`（默认先 `legacy` 灰度，验证后再切）、`COMPONENT_LIBRARY_ENABLED`、`AGENT_MODE=auto|pipeline|off`、`MODEL_PROFILE_STORAGE=disabled|encrypted`、`ALLOWED_MODEL_HOSTS`、`SECRET_ENCRYPTION_KEY`、`INTERNAL_API_TOKEN` |
| 灰度顺序 | ① 上 P0（无行为变化）→ ② `GENERATION_POLICY=legacy` + 组件入库开启（只写不读，攒数据）→ ③ 切 `reuse` 只对 `public` 库生效 → ④ 开广场 → ⑤ 开用户自有模型 |
| 前端/后端 | OCRServer 未升级前，Python 新能力不可见但**不报错**（缺省字段全部可选）；这保证三个工程可以分开发布 |

---

## 8. 分阶段实施计划

> 每阶段独立可验收、可回滚。工时为**粗估**（按单人投入计），仅供排期参考。

### P0 地基：安全与结构（无行为变化）— 约 2–3 天

- 重写为第 6 节分包结构，逐接口迁移，保证 SSE 与响应字节级兼容；
- **内部鉴权**：`X-Internal-Token` 共享密钥 + `X-User-Id` + 时间戳 + HMAC 签名，校验失败 401（关闭伪造 userId 的越权面）；
- 日志脱敏过滤器；错误响应改为安全描述；
- 修 1.3 的 #2（`MILVUS_USER/PASSWORD` 补全并传入 `VectorStore`）、#3（默认模型名）、#4（异常回显）；
- `.env.example` 清空疑似真实 Token，`.env` 拆分本地/生产（本机当前指向 compose 内部主机名，无法启动）；
- 迁移器 + `schema_migrations`；健康检查改为**真实探活**（MySQL 执行 `SELECT 1`、Milvus `list_collections`、嵌入模型一次极小调用），失败返回 `degraded` 而不是恒 `ok`；
- 鉴权以 **`INTERNAL_AUTH_MODE=warn` 上线**（只告警不拒绝），待 OCRServer 带签名后再切 `enforce`（§14.2 #1）；
- 包名落地为 `agent_service/`（PM2 期望的路径），根 `run.py` 保留为兼容壳（§6）；
- 重写期间**旧版本继续占 8000**，新版本跑 8001 做本地联调与回归（§14.5）。

**验收**：现有 curl 用例全部通过；前端聊天与历史无感知（由 8001 双端口验证得出，不靠推断）；伪造 `userId` 在 `enforce` 模式下被拒；日志与响应中无密钥（自动化断言）；`<生产解释器> -c "import fastapi, langgraph, pymilvus, asyncmy"` 通过。

### P1 组件原子化与向量复用（需求①）— 约 5–7 天

- `ui_components` 集合 schema 化创建（**第一步在生产服务器验证 `INVERTED` 索引与字段配额**，不支持则退化为无标量索引）；
- `component_text.py` / `component_service.py` / 三档策略 / 计量；
- 回填脚本 + 阈值校准脚本（把 3.5 的初值定稿为数据结论）；
- `GET /api/stats/generation` 报表。

**验收**：
- 历史回填后组件库 ≥ 历史生成消息数的 70%（抽取率实测后写死基线）；
- 校准后：同义样本召回 ≥ 80%，误复用（人工抽检 100 次）≤ 2%；
- `reuse` 命中路径 **不调用生成模型**（用上游调用计数验证，不是看日志）；
- 相对 `legacy` 基线，生成 token 成本下降 ≥ 30%（报表口径见 3.8）；
- Milvus 不可用时自动降级 FRESH，对话成功率不下降。

### P2 Agent 化（需求②前半）— 约 3–4 天

- `create_agent` 装配 + 5 工具 + 能力降级 + 附件上下文（4.3）；
- 历史窗口/预算裁剪（按 token 预算取最近 N 轮），超长会话不再线性涨价。

**验收**：给定"找一个类似 XX 的组件再改成深色"，agent 能完成为工具调用链并产出可预览组件；不支持 tools 的模型自动落到 pipeline 且不报错；历史 50 轮的会话输入 token 有上限。

### P3 作品广场（需求②后半）— 约 4–6 天

- 广场 API、版本链、fork、点赞、举报、排序；
- 发布合规静态校验 + 单测（每条规则一个用例）；
- 我的作品 / 相似推荐。

**验收**：发布→广场可见→fork→二次修改→新版本的完整闭环跑通；所有 4.5 的拒绝规则有测试覆盖；越权（改他人组件）被拒。

### P4 用户自定义模型（需求③）— 约 3–4 天

- 内联 `modelConfig` 路径（默认）+ 可选加密配置档；
- SSRF 校验 + 单元测试（含 `::ffff:127.0.0.1`、`169.254.169.254`、重定向、非 https 用例）；
- 工厂/缓存/能力降级/usage 估算；`/api/models` 增加"我的模型"来源。

**验收**：用户填错 Key → 返回安全错误信息（不含上游原文）；内网地址被拒且有测试；同模型名不同 Key 不串用（并发测试）；进程内存中实例数有上限；日志零密钥。

### P5 测试与验收 — 与各阶段并行，收口约 2 天

- `pytest` + `pytest-asyncio` + `httpx`（ASGI 直连，无需真起服务）；
- 端到端冒烟脚本（PG/Milvus 可用时跑全链路）；
- 更新 `README.md` / `AGENT.md` / `CLAUDE.md`（新结构、新接口、新 env、新开关）、`Dockerfile`（`COPY app ./app`，现在是逐个文件 COPY，必须同步）、`docker-compose.yml`（新 env）。

**总量级**：约 19–26 天（单人）。若只做 P0+P1（先验证"省 token"这个核心假设），约 7–10 天即可拿到数据再决定后续。

---

## 9. 依赖变更清单

| 依赖 | 状态 | 用途 | 必需性 |
| --- | --- | --- | --- |
| `langgraph` | 已装（`1.2.11`，传递依赖） | Agent 运行时 | 建议**显式**声明，避免上游升级被动丢失 |
| `pydantic-settings` | 已装 | 配置分层 | 建议显式声明 |
| `tiktoken` | 已装 | 无 usage 端点时的 token 估算 | 建议显式声明 |
| `asyncmy` | **缺失** | Python 侧访问 MySQL（C2 定稿，替代原计划的 PG 方向） | P0 必需；不引 SQLAlchemy 也可，直接 asyncmy 更轻 |
| `pymysql` | **缺失** | 迁移脚本/`asyncmy` 不可用时的降级（同步） | 可选 |
| `cryptography` | **缺失** | 用户密钥加密落库（路径 B） | 仅 `MODEL_PROFILE_STORAGE=encrypted` 时需要 |
| `pytest`、`pytest-asyncio` | **缺失** | 测试（当前无测试框架） | P5 必需 |
| `langgraph-checkpoint-postgres` | **缺失** | 会话断点续跑 | 可选，首版不用 |
| `langchain-anthropic`、`langchain-google-genai` | **缺失** | 非 OpenAI 兼容厂商 | 可选，默认不启用 |

**安装前置条件（实测）**：PyPI 清华镜像直连可达（HTTP 200），但环境变量 `all_proxy=socks5://127.0.0.1:7890` 指向一个**没有监听**的端口；`uv add` 会走该代理而失败。安装前需 `unset all_proxy`（或恢复代理）。这一条也解释了为什么本机现在连不上 Milvus / PG。

---

## 10. 必须由你拍板的决策项

| # | 决策 | 选项 | 我的建议 |
| --- | --- | --- | --- |
| D1 | Agent 运行时 | A. `langchain.agents.create_agent`（已装）／B. 手写工具循环 | **A**：少代码、边界清楚；代价是绑 LangChain 版本 |
| D2 | 用户密钥存储 | A. 仅内联传参不落库／B. 加密落库（需装 `cryptography` + 主密钥管理） | **A 先行**，B 作为开关后置：不先背密钥托管责任 |
| D3 | 会话真相源 | ~~A. 双写／B. 按字段拆~~ | **已定：C2 —— 干掉 PG，单库 MySQL 按表域分工；会话域归 OCRServer，组件资产域归本服务。详见 §12、§13** |
| D4 | 广场首版范围 | A. 全量（发布/列表/详情/fork/点赞/举报/版本/审核）／B. MVP（发布/列表/详情/fork/版本） | **B**，点赞举报放 P3 后半：先验证"用户真的会拿别人的组件" |
| D5 | 复用策略默认档 | A. 保守（高置信才复用）／B. 激进（尽量复用） | **A**：误复用的体验损失远大于省下的 token |
| D6 | 是否引入 pytest | A. 引入／B. 沿用现状（无测试） | **A**：SSRF、脱敏、鉴权、复用判定这四类必须靠测试守住，人眼回归不可靠 |
| D7 | 是否现在就处理泄露风险 | `.env.example` 内疑似真实 Milvus Token；伪造 userId 越权 | **建议本轮立即处理**（轮换 Token + 清空模板），不等重构 |
| D8 | 积分为何计 | 用户自带 Key 时是否仍扣积分？ | 需产品决定；Python 侧只提供 usage 数据，规则留在 OCRServer |
| D9 | 已存在的 `works` 表怎么处理 | A. 复用为「广场投影表」+ 新建 `components` 作资产真相源（推荐）／B. 把 `works` 扩展成完整资产表、弃用 `components` | **A**：字段契合度上 `works` 只覆盖展示层，缺 `digest`/`kind`/`category`/向量/版本，硬扩会把展示表变复杂 |

### 风险登记（除 3.10 之外）

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 跨工程耦合被低估 | 广场页面在 OCRServer + 前端，Python 只做后端能力 | 4.6 的对接清单先与两端确认；Python 侧保证"缺省可用" |
| 环境不可验证 | 本机 Milvus 不可达、PG 未运行、代理死后端全灭 | P0 先把健康检查做成真实探活；给一份"本地可跑"的 compose（含 Milvus standalone 可选 profile） |
| 抽取准确率 | 无 AST，正则抽取对复杂输出会漏/错 | 首版接受误差但**必须计量**（抽取率、复用率、误复用率），据此决定是否上 tree-sitter |
| 用户自带模型质量差异 | 弱模型生成的组件可能不合契约，污染组件库 | 入库前静态校验 + 质量分 + 低质量不入 public |
| 广场内容合规 | 公开可见的用户生成代码 | 显式发布 + 静态校验 + 举报 +（建议）人工审核队列 |
| 工作量 | 约 19–26 天，需求③④依赖跨工程排期 | 建议先做 P0+P1 拿数据 |

---

## 11. 本次明确不做的事（边界）

1. 不代改 ocrserver（Node）与前端工程代码，只提供契约与对接清单；
2. 不引入 PG checkpointer、不使用 LangGraph 的持久化图状态（首版）；
3. 不做真正的 TSX AST 解析（tree-sitter 为可选增强）；
4. 不做前端沙箱的运行期隔离（属前端职责，只做服务端静态校验）；
5. 不迁移历史会话的真相源，不改 MySQL 表结构；
6. 不引入 Redis/消息队列等新基础设施（当前部署是 2GB 小内存服务器 + Zilliz Cloud）；
7. 不改变 DeepSeek/Qwen 的平台级凭据来源（仍由 `.env` 提供，仅新增"用户自带"这一层可选覆盖）。

---

## 12. 补充讨论：会话域与资产域该放哪台库（D3 的展开）

> **决策结果：C2 已选定**，落地方案见 §13。本节保留分析过程，作为后续复盘的依据。

> 起因：你提出"MySQL 存用户信息 + 会话索引，PG 存会话历史"。结论是 —— **这个方向对了一半**：对的是"该分"，错的是"分界线画在了实体内部"。

### 12.1 事实基础（本次核实的代码现状）

| 事实 | 证据 |
| --- | --- |
| OCRServer 的 MySQL 里**已经有 `messages` 表**，字段为 `conversation_id / user_id / role / content / model / tokens_used` | `services/chat.js:128-141`（`saveMessage`，用 `INSERT ... SELECT FROM conversations` 顺带做归属校验） |
| MySQL 现有 5 张表：`users`、`conversations`、`messages`、`user_sign_ins`、`credit_transactions` | 全仓库 SQL 语句统计 |
| 两条链路（MiMo 直连 / 转发本服务）**都写 MySQL messages** | `services/chat.js:313` 与 `finalizeMessage` 中的 `saveMessage(..., 'assistant', fullText, finalModel, null)` |
| OCRServer 的历史详情**直接查 MySQL** 返回给前端 | `services/chat.js:459-473` |
| 会话列表只查 `conversations`（标题 + 时间），**不碰 messages** | `services/chat.js:507-530`；前端 `History.tsx:118-119` 也只渲染标题与日期 |
| 本服务 PG 的 `conversations`/`messages` 是 MySQL 的**逐字段副本**（注释即写"字段对齐后端 MySQL 表"） | `database.py:1-40` |

**所以现状不是分工，而是全量双写**：同一个会话、同一批消息，两台库各存一份，且没有任何一处定义"谁是真"。你提的方案会把它变成"按字段拆"，我先说为什么这更贵。

### 12.2 核心判断：按「域」切，不要按「实体字段」切

把**同一个实体**（会话）的索引放 A 库、内容放 B 库，是分布式存储里代价最高的一类切分，因为每一次功能迭代都会同时打在两台库上：

| 一旦要加这些功能（几乎必然） | 后果 |
| --- | --- |
| 列表页显示"最后一条消息预览 / 消息数 / 最后活跃时间" | 索引在 MySQL、内容在 PG → 每页 20 条要么 20 次跨库查询（N+1），要么在 MySQL 里冗余这些字段（**又变回双写**） |
| 建会话 / 删会话 / 清空会话 | 跨库两写，无事务；MySQL 成功、PG 失败 → 孤儿历史；需要补偿任务与对账脚本 |
| 历史详情（现状由 OCRServer 直查 MySQL 提供） | 必须改成代理本服务 → **前端历史功能从此依赖 Python 存活**，多一个故障域 |
| 会话归属校验 | 从"本地一条 SQL"变成跨服务调用（现在 `assertConversationAccess` 是一条本地查询） |
| 备份/恢复 | 两个库必须成对恢复，单独恢复任一个都会数据错位 |
| 回合级元数据（附件、工具轨迹、生成模式） | 逻辑上属于"消息"，但生成者是 Python → 又要在两库之间同步 |

省下来的是**一份消息文本的磁盘**（量很小），付出的是**每次迭代都要跨库协调**（量很大）。对轻量产品，这个交换是亏的。

### 12.3 四个方案对比

| 方案 | 域边界 | 收益 | 代价 | 评价 |
| --- | --- | --- | --- | --- |
| **A. 现状**：两库各存全会话 | 无边界（重叠） | 两个服务各自自洽，读历史都不用跨服务 | 重复存储、无真相源、备份翻倍、可能分叉 | 最差，必须终止 |
| **B. 你的原方案**：MySQL 索引 + PG 内容 | 按字段拆同一实体 | 消除消息重复 | 上表全部问题 + OCRServer 历史详情要改代理 + 前端历史依赖 Python | **不建议** |
| **C1. 会话域整体归 MySQL，PG 只留组件资产域** | 按域切，零重叠 | 会话只有一个真相源；历史详情、鉴权、积分全留在原地不动；PG 专心做组件库（JSONB / pg_trgm 模糊搜索 / UUID / 数组都能用上） | Python 每轮需要历史窗口（内部接口拉取或随请求带上）；Python 不再自带历史副本 | **推荐主线** |
| **C2. 连资产域也放 MySQL，彻底去掉 PG** | 单库多域（表域分工） | 2GB 机器少一个常驻引擎，少一套备份/迁移/连接池 | Python 换 MySQL 驱动；组件模糊搜索改用 MySQL FULLTEXT(ngram)；两个服务共享一个库 schema | **更轻，但有耦合顾虑；内存吃紧时可选** |

### 12.4 推荐路径 C1：具体怎么落

1. **会话域归 MySQL（不改归属）**：`users`、`conversations`、`messages` 全部留在 OCRServer，`messages` 作为唯一真相源继续由它写（包括附图消息的原文）。
2. **本服务删除 `conversations`/`messages` 表与全部读写**（`database.py` 219 行里约 2/3 直接删掉），上下文改为：
   - 首选：内部只读接口 `GET /internal/conversations/{id}/messages?userId=&limit=` 由 OCRServer 提供（一次 HTTP，不占用每轮请求体）；
   - 备选：OCRServer 转发 `/api/chat` 时把最近 N 轮历史窗口一并带上（省一次往返，但每轮重复传文本）。
3. **PG 只保留 Python 独有的域**：`components`、`component_versions`、`component_likes`、`component_forks`、`component_reports`、`component_composition`、`component_usage`、`model_profiles`，以及一张轻量的 `conversation_turns`（见下）。
4. **回合级元数据**（`attachments`、`tool_trace`、生成模式、token 用量）属于 Python 域，放 PG 的 `conversation_turns(conversation_id, turn_index, …)`，用 `(conversation_id, turn_index)` 与会话关联，**不做跨库外键、不做跨库事务**。要在历史里展示这些，由 OCRServer 合并（或前端二次拉取本服务的 `/api/conversations/{id}/turns`）。
5. **历史回填**（3.7）的数据源改为"经 OCRServer 内部接口导出"，而不是读本服务的 PG。

**这个方案唯一变麻烦的点**就是第 4 条：回合元数据与会话实体分属两库，展示时需要合并。这是"按域切"的正常代价，且它不影响正确性（无事务需求，只是展示拼装）。

### 12.5 如果你希望 Python 保持独立自洽（备选 A′）

如果本服务以后要给别的后端复用、或需要独立演进，那么让 Python 自带历史副本是合理的 —— 但**必须把它定义成可重建的派生缓存，而不是第二个真相源**：

- 真相源 = MySQL，**只有 OCRServer 写**（或只有 Python 写 PG，两边二选一，不能都写）；
- PG 副本允许缺失：读不到就回退到内部接口拉取 → 保证正确性不依赖副本；
- 提供 `rebuild_history_cache.py`（从 MySQL 全量重建）与对账脚本（比较行数与最后一条 message id）；
- 文档与代码注释明确标注 `为派生缓存，可随时重建`。

改造量比现状小，比 C1 大；收益是 Python 读历史不用跨服务。

### 12.6 内存与运维量级（选 C1/C2 的判据）

按 compose 里 `postgres:16` 与 MySQL 的**默认配置**估算，两台库各自常驻约 150–250MB（`shared_buffers` / InnoDB buffer pool 默认 128MB 起），即合计 300–500MB 量级；这台服务器是 2GB（compose 注释明确提到内存约束，Milvus 才走 Zilliz Cloud）。

> 这是**量级推算，不是实测**：本机 Docker 当前未运行，无法 `docker stats`。请在服务器上实测一次 `docker stats --no-stream`，把真实 RSS 填进本节。

**判据**：若两台库合计内存占比 > 20%，或团队不想维护两套备份/迁移 → 选 **C2**（Python 直接用 MySQL，`asyncmy` + 表域分工：`components*`/`model_profiles` 归 Python，其余归 Node，各自迁移、禁止跨域写）；否则 **C1** 更好，因为组件库确实吃 PG 的能力（JSONB、`pg_trgm` 中文模糊搜索、`gen_random_uuid`、数组类型）。

> **决策已定：C2**（落地方案见 §13）。上面那次 `docker stats` 实测仍建议做，但用途已从"二选一"变成"量化 C2 省下了多少内存"，结果记回本节。

### 12.7 一句话答复你的问题

> 双 SQL 本身不复杂，复杂的是**让两个库共享同一个实体**。对轻量产品，省事的方向是"一个域一个库、且尽量只留一个库"，而不是"一个实体拆到两个库"。**最终决策：C2** —— 终止全量双写，PG 整体下线，会话域留在 MySQL，组件资产域以"表域分工"的方式并入同一 MySQL（§13）。

---

## 13. C2 定稿：单库表域分工，以及「会话里的组件代码」到底怎么存

> 决策已定：**C2 —— 去掉 PostgreSQL，本服务与 OCRServer 共用同一个 MySQL，按「表的所有权」分工。**
> 本节回答一个具体的落地问题：生成出来的组件代码，是作为对话历史原样存着，还是消息里只留索引、用到时再去资产表现查？

### 13.1 表域分工（谁拥有哪张表）

| 表 | 所有者 | 本服务权限 | 说明 |
| --- | --- | --- | --- |
| `users`、`user_sign_ins`、`credit_transactions` | OCRServer | **无权限** | 身份与计费，本服务完全不碰 |
| `conversations` | OCRServer | 只读 | 会话索引（标题/归属/时间） |
| `messages` | OCRServer | 只读 | **会话内容的唯一真相源**（含生成结果全文） |
| `components`、`component_versions`、`component_composition`、`component_likes`、`component_forks`、`component_reports`、`component_usage` | 本服务 | 读写 | 组件资产域 |
| `conversation_turns` | 本服务 | 读写 | 回合元数据：附件、工具轨迹、生成模式、用量 |
| `model_profiles` | 本服务 | 读写 | 用户模型配置（密文） |
| `works` | **待定（见 §4.4 / D9）** | 建议：本服务读写 | 已存在但无代码引用；建议作为「广场投影表」复用，新增 `component_id` 指回 `components` |
| `orders` | OCRServer/其他 | 无权限 | 已存在、无代码引用，与本方案无关 |

**禁令（三条）**：① 不跨域写（本服务永不 `INSERT/UPDATE/DELETE` `users/conversations/messages`）；② 不建跨服务外键（`conversation_turns.conversation_id` 只是逻辑关联）；③ 不跨服务 JOIN（本服务不 JOIN `users`，作者昵称由 OCRServer 或前端拼接）。

**历史消息的获取方式**：推荐**用独立只读账号直连读 `conversations`/`messages`**（少一跳网络、回填脚本可直接 SQL），归属校验在 Python 侧复刻一句 `WHERE conversation_id=? AND user_id=?`，且 `user_id` 必须来自 P0 的内部鉴权、**不能来自请求体**。若希望边界更严（Python 完全不接触会话表），改走 OCRServer 内部只读接口 `GET /internal/conversations/{id}/messages`，代价是新写一段 Node 代码。

### 13.2 回答你的问题：会话里的组件代码，存全文，不存索引

**结论：`messages.content` 保留当时的完整助手输出（含 ```tsx 代码块）；组件表是派生索引，不是消息的依赖对象。不做"消息只存 componentId、用时去资产表现查"。**

三条理由：

1. **不可变日志不能被可变对象引用**。会话消息是只追加的审计记录（"用户当时看到的字节"）；组件资产是可变对象：可改名、改描述、改可见性、迭代到 v4、下架、被查重合并、被删除。消息一旦只存 `componentId`，作者发个新版本或下架，**历史就被改写了**——用户昨天生成的组件，今天在历史里显示成另一份代码，或者直接 404。
2. **方向反了会把 C2 的解耦重新焊死**。选 C2 的目的就是让组件域的增删改完全不影响会话域的读。消息反向引用组件，等于让组件表的一次 `DELETE`/`UPDATE` 直接改变历史页的呈现。
3. **可重建性会丢失**。当前设计里 **`messages` 是原始真相，`components` 是派生索引**：资产表随时可以 `DROP`，再用 `scripts/backfill_components.py` 从 `messages` 全量重建（§3.7）。反过来（消息引用组件）则让组件表变成真相源，库损坏或换嵌入模型时历史无法重建。

**因此关联方向只放在组件侧**（`components` 已有这两列）：

```sql
source_conversation_id VARCHAR(64),   -- 产出它的会话
source_message_id      BIGINT         -- 产出它的那条 assistant 消息
```

`messages` 表**不加任何组件字段**。需要"从某条历史消息跳到它产出的组件"时由组件侧反查：`GET /api/components?sourceMessageId=…`。这样会话域零改动，前端历史页继续用 `extractJSX` 解析代码块渲染预览，`HistoryDetail.tsx` 不用动。

**代价（要说清楚）**：代码文本会被存两份 —— 一份在 `messages.content`（真相、不可变），一份在 `components.code`（索引、可变）。这个"冗余"是有意为之，且不需要同步（组件表删了也能从消息重建）。量级上完全不值得为它做优化：一个组件约 6KB，10 轮微调会话 ≈ 60KB，1 万次会话 ≈ 600MB 量级。**等真有存储压力再说**，见 13.4。

### 13.3 存储层与上下文层必须分开（这里才是 token 成本的关键）

你的直觉里有一半是对的，只是分错了层：问题不在"存不存全文"，而在"**喂给模型时展开几份**"。

| 层 | 策略 | 原因 |
| --- | --- | --- |
| **存储层**（`messages.content`） | 全文保留，永不改写 | 审计、渲染、回填的唯一依据 |
| **上下文层**（组装 prompt 时） | 只展开**最近一次**的组件代码块（当前基线）；更早的代码块替换为占位摘要，如 `[历史组件 v2：登录卡片，已省略 214 行]` | 10 轮微调会话若把 10 份几乎相同的完整代码全塞进 prompt，输入 token 线性膨胀，正好抵消需求①省下的钱 |

配套：§4.3 的 `sticky` 附件承载"当前基线组件源码"，历史消息里的旧代码块在 prompt 组装阶段一律不展开。这条规则要在 P2 落地时写进 `agent/prompts.py` 与组装函数，并有单测（"50 轮会话的输入 token 不随轮数线性增长"，正是 P2 的验收项）。

### 13.4 唯一值得做的去重：按内容寻址的快照表（可选，P3+，首版不做）

若将来存储真的吃紧，**不改语义**的做法是加一张按内容寻址的表：

```sql
CREATE TABLE IF NOT EXISTS component_snapshots (
    digest     CHAR(64) PRIMARY KEY,      -- 规范化指纹（与 components.digest 同算法）
    code       MEDIUMTEXT NOT NULL,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
);
```

- 同 digest 只存一份 → 多轮微调里重复的代码块自然去重；
- `messages.content` **仍然保留全文**（真相不变）；
- digest 是内容寻址 → 内容天然不可变 → 历史永远可重现，**不违反 13.2 的原则**；
- 收益主要在"历史详情页的传输量"和存储占用，不在 token（token 由 13.3 控制）。

### 13.5 已确认的真实风险：`messages.content` 的列类型 + `user_id` 类型不一致

> **验证结果（本机 MySQL 8.4.9 实测）：`content` 确实是 `TEXT`，风险成立**；同时发现 `user_id` 是 `INT` 外键，与现服务用的 `VARCHAR(64)` 不一致。

核对写库路径时发现：`services/chat.js:313` 保存的是**原始 message 结构体**，图片消息里含 **base64 图片数据**；而 `saveMessage` 会把它 `JSON.stringify` 后写入 `messages.content`。

**已实测确认（本机 MySQL 8.4.9，`SHOW CREATE TABLE messages`）**：

```sql
`content` text COLLATE utf8mb4_unicode_ci NOT NULL,   -- 上限 65535 字节 ← 风险成立
`user_id` int NOT NULL,                               -- ← 与现服务的 VARCHAR(64) 不一致
CONSTRAINT `fk_msg_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
```

- 当前数据量：2 行，最长 42 字节，含 base64 的 user 消息 0 条 → **风险尚未触发，属"迟早会炸"而非"已炸"**。
- 一旦用户上传 512KB 截图（base64 后约 680KB 的 JSON）→ 超出 `TEXT` 上限 → **插入失败（严格模式）或静默截断，图片消息历史丢失**。
- 顺带发现 `messages.content` 列的写入者是 OCRServer，**本服务无权改这张表**（§13.1 禁令）→ 这个修复**属于 OCRServer 的改动项**，必须写进对接清单，不能由本服务擅自 `ALTER`。

**第二个已确认的不一致：用户标识类型**

| | OCRServer（MySQL） | 本服务（现状 / 目标） |
| --- | --- | --- |
| 用户标识 | `user_id INT`（外键 → `users.id`） | 现状 `VARCHAR(64)`，且允许缺省为 `'anonymous'` |
| 会话标识 | `conversation_id VARCHAR(64)` | 同（一致） |

后果：C2 之后本服务直连读 `messages` 时，必须按**数字字符串**处理 `user_id`，**不能再有 `'anonymous'` 这种非数字用户**（否则 `WHERE user_id = 'anonymous'` 既查不到数据、也过不了外键语义）。统一方案：本服务的 `user_id` 一律为 `VARCHAR(64)`，但取值必须是 `users.id` 的十进制字符串；内部鉴权（P0）校验它可解析为整数，拒绝匿名/伪造成本。

**修复建议（按归属分派）**：

| # | 修复项 | 归属 | 状态 |
| --- | --- | --- | --- |
| 1 | `messages.content` → `MEDIUMTEXT`（16MB） | **OCRServer** | ✅ **本轮已完成**（迁移文件 + 本地实测，见 §15） |
| 2 | 图片不再以 base64 进 `messages`（剥离像素，仅存 media_type/字节数） | **OCRServer** | ✅ **本轮已完成**（`services/message_storage.js` + 清理脚本，见 §15） |
| 3 | 超长组件代码（>64KB 的页面） | 本服务 | 自有表里 `code` 用 `MEDIUMTEXT`（参照 `works.source_code` 的既有做法，见 §4.4） |
| 4 | `user_id` 统一为"`users.id` 的十进制字符串" | 双方 | 待 P0：本服务加校验；OCRServer 保持现状 |

> 生产侧仍需执行：迁移 SQL 与清理脚本（步骤见 §15.4）。**本服务无权改 `messages` 表**，因此这两项只能由 OCRServer 侧执行。

### 13.6 MySQL 化后的方言差异（相对 §3.2 / §4.4 的 PG 版本）

| PG | MySQL | 注意 |
| --- | --- | --- |
| `UUID` | `CHAR(36)` | id 由应用层 `uuid4()` 生成，不用 `DEFAULT` |
| `gen_random_uuid()` | 应用层生成 / `UUID()` | 同上 |
| `TIMESTAMPTZ` + `now()` | `DATETIME(3)` + `CURRENT_TIMESTAMP(3)` | **统一 UTC 存储**，见下方时区项 |
| `JSONB` | `JSON` | `attachments`/`tool_trace`/`extra` |
| `TEXT[]`（tags） | `JSON` 数组 | 排序/筛选改应用层处理 |
| `BIGSERIAL` | `BIGINT AUTO_INCREMENT` | — |
| `REAL` | `FLOAT` | 相似度分数 |
| `CHAR(64)`、`VARCHAR(n)`、唯一索引、普通索引 | 同左 | 无需改动 |

**时区对齐是跨工程项（已有实测证据，见 §15.5）**：`created_at` 由 MySQL 的 `CURRENT_TIMESTAMP` 按服务器本地时区写入**墙钟数字**；OCRServer 经 mysql2 驱动把它转成 JS Date，历史接口返回的是 **ISO-8601 UTC**（实测 `2026-10-08T19:05:28.000Z`，而本机墙钟为 `2026-10-09 03:05:28 +0800`）。因此本服务读这些 DATETIME 时**必须显式按 `LEGACY_DB_TIMEZONE`（默认 `Asia/Shanghai`）解释再转 UTC**，否则同一界面上会出现两个相差 8 小时的时间源（历史列表来自 OCRServer，组件广场来自本服务）。P0 定稿该约定。

**迁移执行**：`migrations/mysql/0001_components.sql`、`0002_gallery.sql`、`0003_model_profiles.sql`，由本服务的 `repositories/mysql.py` 迁移器执行；执行账号只需对 `components*`、`conversation_turns`、`model_profiles` 有 DDL 权限，对 `conversations`/`messages` 只有 `SELECT`。

### 13.7 从现状迁到 C2 的步骤

1. 本服务删除 PG 的 `conversations`/`messages` 表与 `database.py` 的会话读写（219 行里约 2/3 直接删掉），历史改为只读账号直连 SQL（13.1）；
2. 本服务新增 MySQL 连接池（`asyncmy`）与迁移器，只连自有域；
3. 资产域表按 13.6 方言建到 MySQL；
4. 若 P1 已上线，把 PG 里已有的组件资产一次性导入 MySQL；PG 中的会话副本**不再使用**（对账后可删）；
5. `docker-compose.yml` 删除 `postgres` 服务与 `pgdata` 卷，`app` 的 `depends_on` 相应调整（MySQL 在宿主机或同 compose）；本服务环境变量由 `POSTGRES_DSN` 改为 `MYSQL_DSN`（或复用 Node 侧 `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` 命名，跨工程更省心）；
6. 保留 PG 数据目录做冷备一个迭代周期，确认无依赖后清理；
7. 上线前先做 13.5 的列类型检查，必要时先扩列再上线。

### 13.8 C2 带来的收益与新增责任（如实记录）

**收益**：少一个数据库引擎（省内存、省一套备份/恢复、省一套迁移路径）；会话域完全不动（OCRServer 的历史/鉴权/积分逻辑零改动，风险最低）；本服务不再维护历史副本，`database.py` 大幅瘦身。

**新增责任**：本服务开始持有 MySQL 连接的凭据与连接池 → 需要 P0 的密钥管理（`MYSQL_DSN` 只从 env 读、不进日志）；两个服务共享一个库 schema → 必须守住 13.1 的三条禁令，并在两个仓库的文档里都写明表域归属，否则半年后会再次长出"跨域写"这种隐性耦合。

---

## 14. 三仓库协同：是否必须同时重构

> **结论：不需要同时重构。** 本服务是目前唯一**尚未部署到生产**的一环（你已确认，后续上线走 PM2），正好可以先把地基（P0）与省 token 的核心假设（P1）独立做完并验证；前端与 OCRServer 在这期间一行代码都不用改。真正需要两侧配合的只有四件小事，见 14.2。

### 14.1 前提事实（已核实）

| 事实 | 含义 |
| --- | --- |
| 前端 + OCRServer 已在运行，前端是唯一有真实用户的入口 | 这两个仓库的改动风险高，能不碰就不碰 |
| 本服务**尚未部署到生产** | 重写没有"打断线上"的压力，等于绿地（greenfield）首发 |
| `OCRServer/.env.production` 与 `.env.development` 均为 `AGENT_SERVICE_ENABLED=true` | **但生产上没有本服务** → 现在直接发 OCRServer 到生产，`/api/chat` 会连不上 `127.0.0.1:8000` 而失败（见 14.2 #2） |
| `ecosystem.config.cjs` 已在 14.4 中改造：入口 `PYTHON_ENTRY`（默认根 `run.py`）、解释器到 `.venv`、目录名可覆盖 | §6 的包名按模板原意定为 `agent_service/`，且部署侧已能两种入口通吃 |
| 本服务与 OCRServer 的耦合面只有 3 个 HTTP 面：`/api/chat`(SSE)、`/api/history*`、`/api/models` | 耦合面小且契约稳定，可以异步推进 |

### 14.2 唯一需要在同一时间窗对齐的四件事

| # | 事项 | 为什么需要同窗 | 解耦手段（把"同时"降级为"可分两次"） |
| --- | --- | --- | --- |
| 1 | 内部鉴权 header（P0） | 本服务开启强制校验后，OCRServer 未带签名 → 全量 401 | 加 `INTERNAL_AUTH_MODE=warn` 宽限期：先只告警不拒绝、正常放行；OCRServer 上线签名后再切 `enforce`。这样两端可分批上线 |
| 2 | 生产开关与首次部署 | `AGENT_SERVICE_ENABLED=true` + 本服务未部署 = 生产 chat 全挂 | 上线前把 `.env.production` 置 `false`；本服务首次部署后同批改回 `true`（一行配置，但必须有人负责） |
| 3 | 时间约定（统一 UTC 存储 + ISO8601 带时区出口） | 数据写进去之后再改要修数据 | **现在还没有数据**（本服务未部署）＝零成本窗口；一旦灰度前必须已定，且 Node 侧要改一处 |
| 4 | 部署入口路径与解释器 | 决定 §6 的包名与依赖安装位置 | 包名已按 PM2 期望定案；解释器不匹配单独解决，见 14.4 |

除这四件外，其余全部可以完全解耦。

### 14.3 分阶段改动矩阵（谁改什么、能否并行）

| 阶段 | 本服务 | OCRServer | 前端 | 并行性 |
| --- | --- | --- | --- | --- |
| P0 地基（鉴权/脱敏/单库/探活） | 大 | 极小（签名 header，也可后加） | 无 | **完全独立**；warn 模式下连 header 都能后置 |
| P1 组件库 + 复用（需求①） | 大 | 无（可选：`done` 事件白名单透传） | 小（"换一个 / 仍用 AI 生成"埋点，可晚两周） | **基本独立**，可首日开工 |
| P2 Agent 化 | 中 | 小（透传 `attachments`/`modelConfig` + SSE 白名单，约半天） | 小（"引入对话"按钮） | 后端薄转发先行 → 本服务 → 前端 |
| P3 作品广场 | 中 | 小（转发 `/api/components/*`） | **大（广场页，关键路径）** | 接口冻结后前端 mock 并行，不等本服务完工 |
| P4 自有模型 | 中 | 小（透传 + 禁日志） | 中（设置页） | 同 P3 |

**关键判断：需求②③的关键路径是前端，不是本服务。** 所以正确策略不是"等前端"，也不是"等本服务"，而是**先冻结契约 → 前端用 mock 并行开发 → OCRServer 做薄转发 → 联调**。

### 14.4 部署问题处理状态

1. **✅ 已处理：解释器与依赖的安装位置（conda → uv）**。`OCRServer/ecosystem.config.cjs` 已改为：
   - 入口：`PYTHON_ENTRY`（默认 `run.py`，与当前仓库结构一致；重构后根目录保留兼容壳，通常无需再改，也可用 `PYTHON_ENTRY=agent_service/run.py` 覆盖）；
   - 解释器：`PYTHON_BIN`（默认 `<工程根>/.venv/bin/python`，即 uv 创建的虚拟环境），已删除 `interpreter: PYTHON_BIN` 指向 conda 的旧路径；
   - 目录名：`PYTHON_DIR_NAME`（默认 `langchainServer`，可用环境变量覆盖）；
   - 三个常量**均可用环境变量覆盖**，改部署不需要动文件；
   - 新增 `PYTHONUNBUFFERED=1`，保证 `pm2 logs agent-service` 能看到实时日志。
   - 依赖安装方式写进文件头注释：`uv sync --frozen` 后验证 `.venv/bin/python -c "import fastapi, uvicorn, langgraph, pymilvus"`。
   - **已实测验证（本机）**：`node --check` 通过；以 `PYTHON_DIR_NAME=langchain1.2_tutorial` 加载配置后，`cwd` / `script` / `interpreter` 三项路径全部存在且解释器可执行（`sys.executable` 打印正常）。
   - ⚠️ 注意：**当前代码仍用 `psycopg` + PostgreSQL**，重构（C2）后才改用 `asyncmy` + MySQL —— 依赖验证名单要按所处阶段调整（现在 `asyncmy` 尚未安装，属预期）。
   - 仍未定的一项：**服务器上 Python 工程目录叫什么**（模板里沿用了原有 `langchainServer`，而本地仓库名是 `langchain1.2_tutorial`）。生产尚未部署，用哪个都可以，届时用 `PYTHON_DIR_NAME=...` 指定即可。
2. **⏳ 待处理：`.env.production` 的 `AGENT_SERVICE_ENABLED`**（14.2 #2）：初次部署本服务前置 `false`，部署成功后再改回 `true`。

### 14.5 推进顺序（含"本地双端口"技巧）

本服务未部署，重写期间**唯一会被影响的是本地联调**：`OCRServer/.env.development` 里 `AGENT_SERVICE_URL=http://127.0.0.1:8000`，前端开发时 chat 依赖它。做法：

- 旧版本继续占 **8000** 保证本地联调不断；新版本跑 **8001**，把本地 `AGENT_SERVICE_URL` 临时指向 8001 验证。**前端与 OCRServer 一行代码都不用改**——这个开关本身就是为这种情况准备的。
- 首次上线复用同一招做蓝绿：新版本先跑 8001（PM2 里加一个进程或用不同端口），全量回归通过后再把生产 `AGENT_SERVICE_URL` 指过去；回滚只需改回端口。

| 周次 | 内容 | 涉及仓库 |
| --- | --- | --- |
| 第 0 周 | 三方各半天：冻结契约（§3.9 / §4.6 / §5.6 + 鉴权 header + 时区 + 部署路径），确认 14.4 | 三个都要开一次会 |
| 第 1–2 周 | 本服务 P0（`INTERNAL_AUTH_MODE=warn`）→ 8001 验证 → 首次部署（PM2） | 仅本服务 |
| 第 3–4 周 | 本服务 P1（组件库 + 复用）+ 前端埋点（可后置）；先 `GENERATION_POLICY=legacy` 只写不读攒数据，再切 `reuse` | 本服务（+ 前端可选） |
| 第 5 周 | OCRServer 转发层（约半天）+ 本服务 P2 + 前端"引入对话" | 三个都要动，但都是小改 |
| 第 5–7 周 | 本服务 P3 与前端广场页**并行** | 本服务 + 前端 |
| 第 8 周 | 本服务 P4 与前端设置页**并行** | 本服务 + 前端 |

### 14.6 一句话回答

不需要同时。本服务是三个仓库里唯一还没上线的一个，**正好可以先单独把 P0+P1 做完**（安全整改 + 省 token 验证），这段时间前端与 OCRServer 完全不用动；进入需求②③之后，也只需按"契约冻结 → 薄转发 → 前端并行"的顺序推进，真正要求两侧配合的只有**签名 header、生产开关、时区、SSE 透传白名单**这四件小事。

---

## 15. OCRServer 侧修复记录（本轮已执行）

> 触发原因：§13.5 的 `messages.content` 溢出风险，本轮先做 OCRServer 侧修复（你指定的顺序：先处理 OCRServer 修复项，再开 P0）。

### 15.1 改动清单（4 个文件，均在 `/Users/CodeGod/backend/OCRServer`）

| 文件 | 类型 | 作用 |
| --- | --- | --- |
| `migrations/001_messages_content_mediumtext.sql` | 新增 | `messages.content` 由 `TEXT` 扩为 `MEDIUMTEXT`；含执行前核对、执行后核对与 DOWN 回滚语句 |
| `services/message_storage.js` | 新增 | 落库前剥离图片 base64 像素的统一规范化函数（**幂等**，保留 media_type 与原始字节数） |
| `services/chat.js` | 修改 | `saveMessage` 改走 `toStorableContent()`（仅 2 行：加 import + 换实现），**四条写库路径全部覆盖**（MiMo 直连的 user/assistant、Agent 转发的 user/assistant） |
| `scripts/cleanup_message_image_payloads.js` | 新增 | 清理历史遗留的 base64 行（默认 dry-run，需 `--apply`；幂等） |

### 15.2 验证证据（全部为实测，非推断）

| 验证项 | 结果 |
| --- | --- |
| 数据库层复现原 bug | 699,163 字节消息 `INSERT` → `ER_DATA_TOO_LONG` (errno 1406) |
| **HTTP 链路复现原 bug** | 真实 `POST /api/chat`（210,000 字节）→ `event: error` `{"error":"Data too long for column 'content'","type":"internal_error"}` |
| 迁移执行 | 用交付的迁移文件执行后，`information_schema` 显示 `content` = `mediumtext`、`NOT NULL` 保持不变 |
| 修复后重测同一请求 | 错误变为 `fetch failed`（下游 Python 未启动，属**预期**），且该消息成功落库 210,000 字节 |
| 规范化函数单测 | 699,182 → **172 字节**（约 4000×）；幂等；文本块、非图片数组、纯文本均不受影响；字节数换算精确（512KB → `bytes: 524288`） |
| 清理脚本三段验证 | 合成遗留行 409,724 字节 → 预演报「1 条 / 可回收 0.39MB」→ `--apply` 后为 166 字节 → 再跑报「需处理 0 条」（幂等） |
| 本地库回归 | 测试前后基线均为 `conversations=1, messages=2`，本轮产生的测试会话已全部删除 |
| 语法与运行时 | `node --check` 三个 JS 文件通过；打补丁后本地服务能正常启动（证明模块路径与导出有效） |

### 15.3 有意接受的副作用（须知情）

MiMo 直连模式（`AGENT_SERVICE_ENABLED=false`）下，**历史轮次的截图不再回放给视觉模型**（当轮图片仍实时交给 MiMo）。
理由：历史页不渲染像素；生成只依赖当轮图片。若将来要恢复，应把图片落到对象存储并在消息里存**引用**，而不是把 base64 写回库。

### 15.4 尚未做（需要你或生产侧执行）

1. **生产库执行迁移** `migrations/001_messages_content_mediumtext.sql`。注意 `TEXT → MEDIUMTEXT` 在 MySQL 8 属**表重建（COPY）**，表越大越慢且会持锁 → 先 `SELECT COUNT(*), MAX(LENGTH(content))` 看体量，必要时放低峰期。
2. **生产库执行清理脚本**：先 `node scripts/cleanup_message_image_payloads.js` 看能回收多少，再 `--apply`。
3. **这 4 个文件要随部署上线**（OCRServer 的部署方式与是否走 git 发布需你确认——仓库只有 13 个受版本控制的文件，`.env.*` 未入库）。
4. ⚠️ **最高优先级：`AGENT_SERVICE_ENABLED` 的生产风险**。仓库内的 `.env.development` 与 `.env.production` 都是 `true`，而生产尚未部署 Python；**服务器上那份 `.env.production` 我无法从这里核实**。若它同为 `true`，**线上聊天此刻就是坏的**（每次请求都连不上 `127.0.0.1:8000`）。请在服务器上执行 `grep AGENT_SERVICE_ENABLED .env.production` 确认，必要时先置 `false`，等本服务首次部署后再改回。

### 15.5 本轮附带发现的时区实证（§13.6 的依据）

| 观测 | 值 |
| --- | --- |
| 本机墙钟 | `2026-10-09 03:05:40 CST (+0800)` |
| MySQL `@@time_zone` / `@@system_time_zone` | `SYSTEM` / `CST` |
| MySQL `NOW()` 经 mysql2 返回 | `2026-10-08T19:05:40.000Z`（即同一时刻的 UTC 表示） |
| `GET /api/history` 返回给前端的 `createdAt` | `2026-10-08T19:05:28.000Z`（ISO-8601 UTC） |

结论：**DATETIME 列里存的是本地墙钟数字，而接口层输出的是 UTC**。本服务若用返回 naive datetime 的驱动直接读并按 UTC 序列化，同一界面上会出现**相差 8 小时**的两个时间源。→ P0 必须落地 `LEGACY_DB_TIMEZONE` 显式转换。
