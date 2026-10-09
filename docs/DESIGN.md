# SnapUI Python 工程重构 · 设计（参考）

> 本文件是**参考设计**：只在设计本身变化时更新。当前状态、决策、进度、下一步看 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。
> 过程记录与原始实测数据归档在 [archive/REFACTOR_PLAN_v1.md](archive/REFACTOR_PLAN_v1.md)。

## 1. 范围

本服务负责：组件生成（DeepSeek）、文档 RAG（Qwen 嵌入 + Milvus）、组件资产化与复用、Agent 工具调用、作品广场后端、用户自带模型接入。
**不在本服务**：鉴权/积分/会话存储（OCRServer）、广场页面与模型设置页（前端）、用户作品的运行期沙箱隔离（前端）。

## 2. 已验证的环境事实

| 项 | 实测结论 |
| --- | --- |
| 本机 MySQL | `127.0.0.1:3306` 直连可用，**8.4.9**，库 `OCRDB`；表：`users` `conversations` `messages` `user_sign_ins` `credit_transactions` `works` `orders` |
| `messages` 结构 | `content` 原为 `TEXT`（**已迁到 `MEDIUMTEXT`**）；`user_id` 是 `INT` 外键指向 `users.id`；`id` 是 `INT`；`sql_mode` 含 `STRICT_TRANS_TABLES` |
| 时区 | MySQL `time_zone=SYSTEM`（CST）；`created_at` 由 `CURRENT_TIMESTAMP` 写**本地墙钟数字**；而 `GET /api/history` 输出的是 **ISO-8601 UTC** |
| Milvus（本地） | 自托管 standalone 改由 compose 栈提供：[deploy/milvus/docker-compose.yml](../../deploy/milvus/docker-compose.yml)，镜像 `milvusdb/milvus:v3.0.0`，端口 19530/9091/2379，数据在**命名卷 `milvus-data`**（全新空库）；旧的 `standalone_embed.sh` + 宿主机 bind mount 链路已废弃（含那份 150MB 旧数据）。需先启动 Docker |
| Milvus（生产） | Zilliz Cloud serverless：`in03-…zilliz.com.cn:443`，**实测可连**；库里只有一个数据库 `db_b15abbec17725da`（`.env` 里的 `MILVUS_DB=snapui_rag` 在该集群**不存在**，云端模式下会被静默忽略）；**集合列表为空**（`design_knowledge` 尚未创建，生产 RAG 知识库当前是空的） |
| Docker | 守护进程常处于停止态 → PG / Milvus 容器随之为停止态 |
| 出站 | 环境变量 `all_proxy=socks5://127.0.0.1:7890` 指向**无监听**端口；`httpx`/LangChain 默认读环境代理会全部失败（本次验证均用 `trust_env=False` 直连）。安装依赖前需 `unset all_proxy` |
| 依赖可用性 | 已装：`langchain 1.3.18`（含 `create_agent`）、`langgraph 1.2.11`、`pymilvus 3.0.1`（含 `create_schema`/`prepare_index_params`/`AsyncMilvusClient`）、`tiktoken`、`pydantic-settings`。缺：`asyncmy`、`cryptography`、`pytest`、`langgraph-checkpoint-postgres` |
| 嵌入模型 | `qwen3.7-text-embedding` 实测可用，**1024 维** |
| 对话模型 | 配置端点 `/models` 实际返回 `deepseek-flash`、`deepseek-v4-pro`；`config.py` 里的默认值 `deepseek-v4-flash` **是错的**，P0 修正 |
| PM2 | `ecosystem.config.cjs` 已改为 `.venv/bin/python` + 根 `run.py`（§3.3） |

## 3. 目标架构

### 3.1 调用链

```
前端 ──JWT──▶ OCRServer（鉴权/积分/MySQL 会话/视觉理解/反向代理）
                   │  内部调用 + 共享密钥签名（X-Internal-Token / HMAC / 时间戳）
                   ▼
        本服务 agent_service/：意图路由 ┬ 直接复用（0 生成 token）
                                        ├ 参考生成（few-shot）
                                        ├ 全量生成
                                        └ 附件改造（引入源码二次修改）
        依赖：MySQL（会话只读 + 自有资产表）· Milvus（检索索引）· 用户自定义 LLM 端点（SSRF 白名单）
```

### 3.2 目录结构（重写后）

```
agent_service/
  run.py                      # 真实入口（PM2 指向这里）
  main.py                     # FastAPI 装配：lifespan / 路由 / 中间件 / 异常处理
  core/       config.py logging.py(脱敏) security.py(HMAC·Fernet·SSRF) errors.py deps.py
  api/        routers/{chat,history,knowledge,components,model_profiles,stats,health}.py  sse.py
  schemas/    按域拆的 pydantic 模型
  services/   chat_service.py generation.py component_service.py component_text.py
              gallery_service.py moderation.py usage.py
  agent/      graph.py tools.py prompts.py  llm/{factory.py,cache.py}
  repositories/ mysql.py conversations.py messages.py components.py model_profiles.py vector_store.py
  migrations/ 0001_components.sql 0002_gallery.sql 0003_model_profiles.sql
scripts/      backfill_components.py reindex_components.py calibrate_thresholds.py e2e_smoke.py
tests/        pytest：SSE 契约 / 复用判定 / 鉴权 / SSRF / 脱敏
run.py        # 兼容壳：转调 agent_service/run.py
```

包名必须是 `agent_service/`：PM2 期望 `../langchainServer/agent_service/run.py`。根 `run.py` 兼容壳保证 `uv run python run.py` 照旧可用。

### 3.3 部署（PM2）

`OCRServer/ecosystem.config.cjs` 中的 `agent-service` 进程：`cwd=<工程根>`、`script=<根>/run.py`、`interpreter=<根>/.venv/bin/python`、`PYTHONUNBUFFERED=1`。
三个常量均可用环境变量覆盖：`PYTHON_DIR_NAME`（默认 `langchainServer`）、`PYTHON_BIN`、`PYTHON_ENTRY`。
上线前：`uv sync --frozen` → `.venv/bin/python -c "import fastapi, uvicorn, langgraph, pymilvus"`（C2 落地后把驱动换成 `asyncmy`）。

## 4. 存储与域边界（C2 定稿）

### 4.1 表域地图

| 表 | 所有者 | 本服务权限 |
| --- | --- | --- |
| `users` `user_sign_ins` `credit_transactions` `orders` | OCRServer | 无 |
| `conversations` `messages` | OCRServer | **只读**（独立只读账号；`SELECT` only） |
| `components` `component_versions` `component_composition` `component_likes` `component_forks` `component_reports` `component_usage` | 本服务 | 读写 |
| `conversation_turns` `model_profiles` | 本服务 | 读写 |
| `works` | 本服务（建议，D9） | 读写（作为广场投影，新增 `component_id`） |

**三条禁令**：① 不跨域写（本服务永不 `INSERT/UPDATE/DELETE` 会话域）；② 不建跨服务外键（`conversation_turns.conversation_id` 只是逻辑关联）；③ 不跨服务 JOIN（作者昵称等由 OCRServer 或前端拼接）。
**迁移执行**：只用自有域表的 DDL 权限，`repositories/mysql.py` 跑 `migrations/*.sql`，全部幂等。

### 4.2 会话域契约（只读）

- 历史消息默认**只读账号直连**（少一跳、回填脚本可直接 SQL）；归属校验在本服务复刻 `WHERE conversation_id=? AND user_id=?`，`user_id` **必须来自内部鉴权**，不能来自请求体。
- 若要更严的边界，改为走 OCRServer 内部接口 `GET /internal/conversations/{id}/messages`（代价：新增 Node 代码）。
- 历史详情仍由 OCRServer 提供，本服务不承担前端历史页的读取职责。

### 4.3 资产域 DDL（MySQL）

```sql
-- 组件主表：资产域真相源（内容可从 messages 重建，见 §4.6）
CREATE TABLE IF NOT EXISTS components (
  id                     CHAR(36)     NOT NULL,               -- 应用层 uuid4()
  owner_user_id          VARCHAR(64)  NOT NULL,               -- users.id 的十进制字符串，禁止 'anonymous'
  name                   VARCHAR(120) NOT NULL DEFAULT '',
  kind                   VARCHAR(16)  NOT NULL,               -- page | composite | atomic
  category               VARCHAR(32)  NOT NULL DEFAULT 'other',
  description            TEXT         NOT NULL,
  language               VARCHAR(16)  NOT NULL DEFAULT 'tsx',
  code                   MEDIUMTEXT   NOT NULL,               -- 参照 works.source_code 的做法
  entry_wrapper          VARCHAR(32)  NOT NULL DEFAULT 'component',
  digest                 CHAR(64)     NOT NULL,               -- 规范化指纹 sha256
  semantic_text          VARCHAR(2048) NOT NULL DEFAULT '',   -- 被嵌入的检索串（与查询侧同规则构造）
  embedding_model        VARCHAR(64)  NOT NULL DEFAULT '',
  embedding_dim          INTEGER      NOT NULL DEFAULT 1024,
  visibility             VARCHAR(16)  NOT NULL DEFAULT 'private',  -- private|unlisted|public
  status                 VARCHAR(16)  NOT NULL DEFAULT 'draft',    -- draft|published|removed|rejected
  quality_score          FLOAT        NOT NULL DEFAULT 0,
  reuse_count            INTEGER      NOT NULL DEFAULT 0,      -- 被直接复用次数
  ref_count              INTEGER      NOT NULL DEFAULT 0,      -- 被当作参考次数
  like_count             INTEGER      NOT NULL DEFAULT 0,
  fork_count             INTEGER      NOT NULL DEFAULT 0,
  fork_of                CHAR(36)     NULL,
  source_conversation_id VARCHAR(64)  NULL,
  source_message_id      INT          NULL,                    -- 与 messages.id 同为 INT
  created_at             DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at             DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  published_at           DATETIME(3)  NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_components_owner_digest (owner_user_id, digest),
  KEY idx_components_owner   (owner_user_id, created_at),
  KEY idx_components_gallery (visibility, status, published_at),
  KEY idx_components_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 组合关系（父由哪些原子组成）
CREATE TABLE IF NOT EXISTS component_composition (
  parent_id CHAR(36) NOT NULL, child_id CHAR(36) NOT NULL, slot VARCHAR(64) NOT NULL DEFAULT '',
  PRIMARY KEY (parent_id, child_id, slot)
) ENGINE=InnoDB;

-- 版本链（迭代不覆盖历史）
CREATE TABLE IF NOT EXISTS component_versions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, component_id CHAR(36) NOT NULL, version INT NOT NULL,
  code MEDIUMTEXT NOT NULL, digest CHAR(64) NOT NULL, changelog VARCHAR(500) NOT NULL DEFAULT '',
  created_by VARCHAR(64) NOT NULL, created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_component_version (component_id, version)
) ENGINE=InnoDB;

-- 点赞 / fork / 举报
CREATE TABLE IF NOT EXISTS component_likes (
  component_id CHAR(36) NOT NULL, user_id VARCHAR(64) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3), PRIMARY KEY (component_id, user_id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS component_forks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, source_component_id CHAR(36) NOT NULL,
  new_component_id CHAR(36) NOT NULL, user_id VARCHAR(64) NOT NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS component_reports (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, component_id CHAR(36) NOT NULL, reporter_user_id VARCHAR(64) NOT NULL,
  reason VARCHAR(500) NOT NULL DEFAULT '', status VARCHAR(16) NOT NULL DEFAULT 'pending',
  handled_by VARCHAR(64) NULL, created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3), handled_at DATETIME(3) NULL
) ENGINE=InnoDB;

-- 计量（复用命中与 token 账本）
CREATE TABLE IF NOT EXISTS component_usage (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, component_id CHAR(36) NULL, conversation_id VARCHAR(64) NULL,
  user_id VARCHAR(64) NULL, mode VARCHAR(16) NOT NULL, similarity FLOAT NULL,
  prompt_tokens INT NOT NULL DEFAULT 0, output_tokens INT NOT NULL DEFAULT 0,
  est_saved_tokens INT NOT NULL DEFAULT 0, model VARCHAR(128) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_component_usage_time (created_at)
) ENGINE=InnoDB;

-- 回合元数据（附件 / 工具轨迹 / 生成模式），替代给 messages 加列
CREATE TABLE IF NOT EXISTS conversation_turns (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, conversation_id VARCHAR(64) NOT NULL, turn_index INT NOT NULL,
  user_id VARCHAR(64) NOT NULL, attachments JSON NOT NULL, tool_trace JSON NOT NULL,
  generation_mode VARCHAR(16) NOT NULL DEFAULT 'fresh', model VARCHAR(128) NULL,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_turn (conversation_id, turn_index)
) ENGINE=InnoDB;

-- 用户模型配置（明文永不入库；D2 决定是否启用）
CREATE TABLE IF NOT EXISTS model_profiles (
  id CHAR(36) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, label VARCHAR(64) NOT NULL,
  provider VARCHAR(32) NOT NULL, base_url VARCHAR(512) NOT NULL, model_name VARCHAR(128) NOT NULL,
  api_key_cipher BLOB NULL, key_last4 VARCHAR(8) NULL, extra JSON NULL, is_default TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_profile_label (user_id, label)
) ENGINE=InnoDB;
```

`works` 扩展（D9）：加 `component_id CHAR(36) NULL` + 索引；`works` 继续存展示字段（`title`/`description`/`preview_image`/`tags`/`is_public`）。

### 4.4 Milvus：双环境（本地 standalone / 生产 Zilliz Cloud）

**环境矩阵（均为实测）**

| 项 | 本地 | 生产 |
| --- | --- | --- |
| 形态 | 自托管 standalone（`milvusdb/milvus:v3.0.0`，Docker） | Zilliz Cloud serverless 集群 |
| 连接 | `MILVUS_URI=http://localhost:19530`，无鉴权 | `MILVUS_URI=https://in03-…zilliz.com.cn` + `MILVUS_TOKEN` |
| 数据库 | 可 `create_database` + `use_database`（`MILVUS_DB=snapui_rag`） | 只有 `db_b15abbec17725da`，**无权建库**；`MILVUS_DB` 必须留空或填该库名 |
| 集合现状 | 全新空库（命名卷 `milvus-data`） | **空**（`list_collections()` → `[]`） |
| 建集合 | schema 模式 | schema 模式**实测可用**（3.0s） |
| 标量索引 | 预期支持 | **`INVERTED` 实测支持**（`list_indexes` 返回 `['category','vector']`） |
| 不 flush 可搜 | 未测 | **实测可以**（插入后 t+0s 即命中，带过滤也命中） |

**配置契约**（P0 落地，替换现在含糊的 `managed = bool(user or token)` 启发式）

| 变量 | 说明 |
| --- | --- |
| `APP_ENV` | `local` \| `production`；决定加载 `.env.local` 还是 `.env`（当前只有一个 `.env`，且**指向云端**，见下方防误连） |
| `MILVUS_URI` / `MILVUS_TOKEN` | 连接与鉴权；本地留空 token |
| `MILVUS_USER` / `MILVUS_PASSWORD` | **现状 bug**：`docker-compose.yml` 注入了它们，但 `config.py` 不读、`vector_store` 也不传 → 静默失效。P0 补上（用户名/密码优先于 token） |
| `MILVUS_DB` | 本地=库名（不存在则创建）；云端=留空或集群内既有库名。**不得再用 `snapui_rag` 这类只存在于本地的名字** |
| `MILVUS_MODE` | `auto`（默认）\| `standalone` \| `cloud`；`cloud` 时跳过建库、并在连接后**校验当前库名**，不符则启动即失败并给出明确提示 |
| `MILVUS_TIMEOUT` / `MILVUS_MAX_RETRIES` | 云端需显式超时与重试（本地几乎无延迟）；建议 10s / 2 次退避 |
| `MILVUS_ALLOW_REMOTE_IN_LOCAL` | 默认 false 的**防误连闸门**：`APP_ENV=local` 时若 URI 主机不是 localhost/私网 → 拒绝启动。**因为现在 `.env` 就是指向云端的**，本地随手跑一次脚本就可能往生产集群写集合 |

**集合与建表（schema 模式）**

沿用 `design_knowledge`（文档 RAG）；新增 `ui_components`（组件检索）：

```python
schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
schema.add_field("id",            DataType.VARCHAR, is_primary=True, max_length=64)
schema.add_field("vector",        DataType.FLOAT_VECTOR, dim=1024)
schema.add_field("kind",          DataType.VARCHAR, max_length=16)
schema.add_field("category",      DataType.VARCHAR, max_length=32)
schema.add_field("visibility",    DataType.VARCHAR, max_length=16)
schema.add_field("status",        DataType.VARCHAR, max_length=16)
schema.add_field("owner_user_id", DataType.VARCHAR, max_length=64)
schema.add_field("semantic_text", DataType.VARCHAR, max_length=2048)
index = client.prepare_index_params()
index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
index.add_index(field_name="category", index_type="INVERTED")   # 云端已实测支持；仍保留 try/except 降级
client.create_collection(collection_name="ui_components", schema=schema, index_params=index)
```

初始化流程（幂等）：`standalone` → 需要时建库并 `use_database`；`cloud` → 不建库，校验当前库名；两者都 `has_collection` → 缺则建；建完 `load_collection` 并**轮询真实装载状态**。

**⚠️ 两个实测出来的坑（必须在代码里处理）**

1. **装载未就绪会造成"空结果假阴性"**：第一次探测时，`create_collection` 后立刻 `load_collection` 就插入并检索 → **0 命中**；第二次在 `load_collection` 后等待约 2s 再插入 → **立刻命中**。所以：
   - 不要用 `str(get_load_state(...))` 做子串匹配（`"{'state': <LoadState: Loaded>}"` 恰好包含 `LOADED`，会误判为已就绪）→ 解析出 `LoadState` 枚举再比较；
   - 复用检索在返回空时**必须重试有限次**（如 3 次 / 200ms 间隔），不能把"没就绪"当成"库里没有"——那会静默降级为 FRESH，白花 token。
2. **每次 upsert 都 `flush()` 是多余的**（现 `vector_store.upsert_texts` 每次都 flush）：实测不 flush 也能立刻检索到。云端按 CU 计费，逐条 flush 是纯浪费 → 改为：单条/少量写入不 flush；批量导入（回填、重建索引）每 N 条 flush 一次；进程优雅退出前 flush 一次。**flush 与可检索性无关，别把它当"提交"。**

过滤表达式示例：`category in ["action","form"] and status == "published" and visibility in ["public","unlisted"]`。
Milvus 只存 id + 向量 + 可过滤标量，**不存源码**（源码在 MySQL，可随时用 `reindex_components.py` 重建）。

**知识库现状提醒**：生产集群集合为空 → 上线后 `design_knowledge` 是空集合，RAG 会"检索不到东西但不报错"。需要一次性 ingest（**推荐向生产集群重新 ingest，不要搬存储文件**）。

**两端各自的验证命令**（P0 的验收项）

```bash
# 本地：先起 Docker Desktop，再拉起 compose 栈（之后可在 GUI 里启停）
docker compose -f deploy/milvus/docker-compose.yml up -d
.venv/bin/python -c "from pymilvus import MilvusClient; c=MilvusClient('http://localhost:19530'); print(c.list_collections())"
# 云端：只读探测（不要建集合）
.venv/bin/python -c "import os;from dotenv import load_dotenv;load_dotenv();from pymilvus import MilvusClient;c=MilvusClient(uri=os.environ['MILVUS_URI'],token=os.environ['MILVUS_TOKEN']);print(c.list_databases(), c.list_collections())"
```

### 4.5 键与时间契约

| 契约 | 内容 |
| --- | --- |
| `user_id` | 一律为 `users.id` 的十进制字符串；内部鉴权校验其可解析为整数；**禁止 `'anonymous'`**（`messages.user_id` 是 `INT` 外键） |
| `conversation_id` | `VARCHAR(64)`，与 OCRServer 一致 |
| 时间 | OCRServer 的 `DATETIME` 是**本地墙钟**（CST）；本服务读取时按 `LEGACY_DB_TIMEZONE`（默认 `Asia/Shanghai`）解释并转 UTC；对外出口统一 ISO-8601 带时区 |
| 向量 | `embedding_model` + `embedding_dim` 记录在行上；模型或维度变化必须走 `reindex_components.py`，禁止混用 |
| 指纹 | `digest` = 规范化（去注释/空白、统一引号、属性排序）后的 sha256；用于精确去重与版本判定 |

### 4.6 存储层 vs 上下文层（`messages` 与 `components` 的关系）

- **`messages` 是原始真相**（用户当时看到的字节，只追加、永不改写）；**`components` 是派生索引**，可随时 `DROP` 并由 `backfill_components.py` 从消息重建。
- 关联方向**只在组件侧**（`source_conversation_id` / `source_message_id`），消息表**不加任何组件字段**：否则组件改名/下架/合并会让历史被改写。要"从历史消息跳到组件"就反查 `GET /api/components?sourceMessageId=`。
- **存储层保留全文，上下文层必须裁剪**：组装 prompt 时只展开**最近一次**的代码块（当前基线），更早的替换为占位摘要（`[历史组件 v2：登录卡片，已省略 214 行]`）。否则 N 轮微调会把 N 份几乎相同的代码塞进 prompt，抵消复用省下的 token。
- 代码文本确实会存两份（消息一份、组件一份），**有意为之且无需同步**；量级约 6KB/组件，不做压缩优化。

## 5. 需求①：组件原子化与向量复用

### 5.1 资产模型

粒度 `kind`：`page`（一次生成的完整结果，含入口 `Component`）/ `composite`（组合块）/ `atomic`（原子件，复用主命中对象）。
类别 `category`（封闭枚举，检索前过滤）：
`layout`(navbar sidebar footer container grid tabs breadcrumb) · `form`(input textarea select checkbox radio switch date-picker form-group login-form) · `action`(button icon-button dropdown menu) · `display`(card table list badge avatar tag tooltip modal drawer toast accordion pagination progress skeleton chart image) · `feedback`(alert spinner empty-state)

原子性判定（零 token 规则）：代码块解析出各函数组件 → 函数体 < 5 行且无 props/状态则并入宿主 → 引用其他自定义组件或含根布局容器则降级为 `composite`/`page` → 其余按关键词（优先）或向量类心归入 `category`。
**局限（诚实说明）**：Python 侧没有 TSX AST，首版用"代码块正则 + 规范化指纹 + 关键词/向量分类"，对动态生成与复杂导出有误差；可选增强是 `tree-sitter` + `tree-sitter-typescript`，默认不启用。

### 5.2 检索串（查询侧与入库侧必须同一函数）

`build_semantic_text()` 单点实现，两侧规则一致：
- 入库：`"{kind}/{category} | {name} | {description} | 属性: {attrs} | 风格: {style}"`
- 查询：`"{category_hint}/{kind_hint} | {归一化需求} | 属性: {attrs} | 风格: {style}"`
- 归一化：剥离「帮我/请/给我/生成一个/做一个」等口语前缀、压缩空白、全半角统一、去重。
- 属性抽取（正则词表，零 token）：颜色、尺寸、形状、状态（hover/禁用/加载）、布局；风格 token：极简/拟物/玻璃拟态/暗色/扁平。

### 5.3 三档策略

```
1) 意图路由（规则优先）：附件含源码 或 命中「改成/调整/换成/加一个/去掉/把…改成…」且最近 AI 消息含代码块 → ADJUST
2) CREATE：q = build_semantic_text(需求); hits = search(q, filter, limit=8); top = hits[0]
     top.score ≥ T_REUSE 且 structure_ok 且 specificity_ok → REUSE（不调用生成模型，直接返回 top.code）
     T_REF ≤ score < T_REUSE                              → REFERENCE（top1..3 源码作 few-shot）
     否则                                                  → FRESH（现状全量生成）
3) 统一收尾：抽取子组件 → 指纹去重 → 入库 → 记 component_usage
```

- `structure_ok`：类别一致；需求属性与候选 `semantic_text` 属性无冲突（需求"红色"、候选"蓝色" → 拒绝）；通过 §6.5 静态校验；`status='published'`。
- `specificity_ok`：需求至少含 1 个属性或结构约束才允许直接复用——无约束需求（"生成一个按钮"）与具体变体相似度只有 0.58，会命中"某个具体样式"，用户拿到的可能不是想要的；否则降级 REFERENCE 或由前端给候选。

### 5.4 阈值与实测依据

| 样本对 | 实测相似度 | 判定 |
| --- | --- | --- |
| 「蓝色主色圆角按钮带 hover」↔「按钮组件，圆角、蓝色背景、悬停变化」 | 0.9123 | 可复用 |
| 同上 ↔ 英文同义句 | 0.9066 | 可复用 |
| 「顶部导航栏含 logo 和搜索框」↔「Navbar，左 logo 右搜索框，固定顶部」 | 0.8511 | 参考生成 |
| 「生成一个按钮」↔「帮我生成一个按钮组件」 | 0.8942 | 边界（被 specificity 压制） |
| 蓝圆角按钮 ↔ 红方形按钮（属性冲突） | 0.5544 | 不可复用 |
| 跨类（按钮↔导航 / 按钮↔表单） | 0.39–0.52 | 不可复用 |

初值：**`T_REUSE=0.90`、`T_REF=0.80`、`T_DUP=0.97`**。样本仅 10 句，不足以定稿 → P1 必须跑 `calibrate_thresholds.py`（≥100 对标注样本，扫阈值画 PR，把 `T_REUSE` 定在「误复用率 ≤ 2%」处）。首版取保守值。

### 5.5 入库 / 去重 / 版本

1. 抽取候选组件 → 2. 规范化指纹 sha256 → 3. 精确去重：`(owner_user_id, digest)` 命中则 `reuse_count += 1` → 4. 近似去重：top1 ≥ `T_DUP` 走 `component_versions` 新版本；跨用户则标 `fork_of` → 5. 默认 `private`+`draft`，**只有用户显式发布才 public** → 6. `quality_score = f(静态校验通过, 复用次数, 点赞−举报, 无障碍属性)`，复用优先选高分者。

### 5.6 冷启动与重建

- `backfill_components.py`：遍历 assistant 消息 → 抽取 → 指纹/向量 → 入库（幂等）。历史消息是现成语料，让复用率在灰度当天就有意义。
- `reindex_components.py`：以 MySQL 资产表为准全量重建 Milvus 集合（换嵌入模型、集合损坏、迁移实例）。
- `calibrate_thresholds.py`：从会话历史取标注样本做阈值校准。

### 5.7 计量（"省了多少 token"的可验证口径）

每次对话写一条 `component_usage`：`mode`（reuse/reference/fresh/adjust）、`similarity`、`prompt_tokens`、`output_tokens`、`est_saved_tokens`。
基线均值来自 `GENERATION_POLICY=legacy` 期间的统计；`reuse` 记"若走 REFERENCE 的基线输出"，`reference` 记"FRESH 基线 − 实际输出"。
`GET /api/stats/generation?days=7` 返回各档占比、平均 token、估算节省、复用命中率、误复用回退率（前端「换一个」点击率是唯一可信的误复用信号，必须埋点）。

### 5.8 复用结果如何返回前端

实测：OCRServer 的 SSE 解析器只认 `data:` 行里的 `text`/`error`/`stopReason`，忽略事件名，并重新编码后转发 → 本服务在 `done` 事件里新增 `mode`/`componentId`/`similarity`/`reused` **不会破坏解析**；但前端要看到这些字段，OCRServer 必须加白名单透传（§6.7）。
复用返回的代码必须仍满足前端契约：函数名 `Component`、无 `export`、跳转用 `Link/useNavigate`（禁用 `window.location`/`BrowserRouter`）→ 命中 `atomic` 时按 `entry_wrapper` 包装后再返回。

## 6. 需求②：AI Agent 与作品广场

### 6.1 Agent 运行时

`langchain.agents.create_agent`（已验证可用，基于 `langgraph 1.2.11`）：

```python
agent = create_agent(model=chat_model, tools=[...], system_prompt=SYSTEM_PROMPT,
                     checkpointer=None, middleware=[])
```

- **不引入 PG checkpointer**：会话真相源是 MySQL `messages`，保持「每轮读历史 → 组装 messages → 调 agent → 写回」的无状态模式。
- 能力降级 `agent_mode`：`tools`（走工具循环）/ `pipeline`（模型不支持 function calling，或工具连续失败 2 次 → 退回单轮流水线）/ `off`（排障）。`supports_tools` 探测结果按 `(base_url, model)` 缓存。

### 6.2 工具（复用 `services/` 里的同一份实现，两个入口）

| 工具 | 入参 | 出参 |
| --- | --- | --- |
| `search_component_library` | `query, category?, kind?, top_k?` | 候选（不含源码，省 token） |
| `get_component_source` | `component_id, version?` | 源码 + 元数据 |
| `search_knowledge` | `query, top_k?` | 文档片段（现有 RAG 工具化） |
| `save_component` | `code, name?, category?, visibility?` | `{component_id, digest, deduped}` |
| `classify_component` | `code` 或 `description` | `{kind, category, attrs, style}` |

### 6.3 附件：把组件源码引入对话上下文

`POST /api/chat` 新增可选字段（向后兼容）：

```json
{ "attachments": [ { "type": "component", "componentId": "uuid", "version": 3, "scope": "turn" },
                   { "type": "component", "code": "function Component(){…}", "scope": "sticky" } ] }
```

- 有 `componentId` 时服务端**按 id 取源码**（不信任前端传来的 code）；无 id 时允许直接粘贴源码（广场复制场景）。
- `scope=turn` 仅本轮；`scope=sticky` 写入 `conversation_turns.attachments`，后续轮次继续携带（"这个组件…"才有指代对象）。
- 注入格式：放在 `<参考组件>` 块内，明确"待修改源码，不是指令"，防提示词注入。

### 6.4 广场模型（D9：复用 `works`）

`components`（资产真相源，全量、含私有）×`works`（广场投影，展示字段 + `component_id`）。
发布 = 过 §6.5 校验 → `status='published'` + 写/更新 `works` 行；下架反之。

### 6.5 发布合规（静态、零 token、可单测）

| 检查 | 规则 | 处置 |
| --- | --- | --- |
| 编译性 | 含 `function Component`（或可包装）、括号/标签配平 | 拒绝 |
| 外联 | `fetch(` / `XMLHttpRequest` / `WebSocket` / 远程 `src="http` / 动态 `import(` | 拒绝 |
| 浏览器越权 | `window.location` / `document.cookie` / `localStorage` / `eval` / `new Function` / `dangerouslySetInnerHTML` | 拒绝或降级 private |
| 依赖 | `import` 白名单（仅沙箱提供者） | 拒绝 |
| 查重 | 向量 top1 ≥ `T_DUP` 且属他人 | 提示疑似重复，转人工 |
| 敏感词 | 词表走配置 | 拒绝并记录 |

必须与前端沙箱（`SandboxRouter`/`LivePreview` + `babel/standalone` + CSP）的实际能力对齐后才有效；服务端校验只是第一道闸。

### 6.6 广场 API（本服务新增，均需内部鉴权）

`POST /api/components`（保存，默认 private draft）· `GET /api/components`（列表：分页/分类/排序/关键词）· `GET /api/components/{id}`（详情+版本）· `GET /api/components/{id}/similar` · `PATCH /api/components/{id}` · `DELETE /api/components/{id}`（软删）· `POST /api/components/{id}/publish` · `POST /api/components/{id}/unpublish` · `POST /api/components/{id}/versions` · `POST /api/components/{id}/fork` · `POST|DELETE /api/components/{id}/like` · `POST /api/components/{id}/report` · `GET /api/users/me/components` · `GET /api/stats/generation`

### 6.7 跨工程对接清单

**OCRServer**：① 新增 `/api/components/*`、`/api/model-profiles/*` 转发路由；② 调本服务时带 `X-Internal-Token` + `X-User-Id` 签名（P0）；③ `/api/chat` 的 SSE 解析器增加白名单透传（`mode`/`componentId`/`similarity`/`reused`）；④ 透传 `attachments` / `modelConfig`；⑤ 积分规则调整（D8）。
**前端**：① 广场页；② 「引入对话」按钮（走 attachments）；③ 模型设置页；④ 复用结果提示 + 「换一个 / 仍用 AI 生成」埋点；⑤ 沙箱运行期隔离（iframe + CSP）与服务端校验对齐。

## 7. 需求③：用户自定义 APIKey / BaseURL / ModelName

### 7.1 两条路径

| 路径 | 说明 | 建议 |
| --- | --- | --- |
| **A 内联传参** | 前端本地存 Key，随请求 `modelConfig` 上传；服务端不落库、不写日志 | **首版必做** |
| B 配置档 | `POST /api/model-profiles` 加密落库，请求里用 `modelProfileId` 引用 | 可选开关，默认 `disabled` |

优先级：请求内联 > `modelProfileId` > 服务端默认（`.env`）；全部缺省时行为与现状一致（OCRServer 不改也能跑）。

### 7.2 加密与脱敏

- `cryptography` 的 Fernet（AES-128-CBC + HMAC），主密钥来自 `SECRET_ENCRYPTION_KEY`（base64 32B，仅 env）；缺失则强制 `MODEL_PROFILE_STORAGE=disabled` 并启动告警。
- 读取只回掩码 `sk-****<last4>`；任何接口不返回明文。
- 日志过滤器正则替换：`sk-[A-Za-z0-9_\-]{8,}`、`Bearer\s+\S+`、`(?i)(api[_-]?key|token|authorization|password)\s*[=:]\s*\S+`；SSE `error` 只回错误码 + 安全描述（现状 `main.py` 直接回显 `str(exc)`，P0 修）。

### 7.3 SSRF 清单（用户可控 `baseUrl` 是最大风险面）

① 仅 `https`（`ALLOW_INSECURE_MODEL_URL` 默认 false）；② 解析 A/AAAA 后逐 IP 用 `ipaddress` 拒绝 private/loopback/link-local/reserved/multicast/unspecified，**含 IPv4-mapped IPv6**（`::ffff:127.0.0.1`）；③ 校验后把 IP **固定到连接层**（防 DNS rebinding；禁止"校验一次、再用域名连一次"）；④ `follow_redirects=False`；⑤ `ALLOWED_MODEL_HOSTS` 白名单（生产建议开）；⑥ 超时 connect 5s / read 60s、单用户并发与 QPS 上限；⑦ 失败只回"端点不可达"，不回显内网响应体。

### 7.4 模型工厂与实例缓存

```python
if cfg.provider == "openai-compatible":   # 覆盖绝大多数中转/自建端点
    return ChatOpenAI(model=cfg.model_name, api_key=cfg.api_key, base_url=cfg.base_url,
                      timeout=cfg.timeout, max_retries=1, stream_usage=True)
if cfg.provider == "deepseek":
    return ChatDeepSeek(model=cfg.model_name, api_key=cfg.api_key, api_base=cfg.base_url, stream_usage=True)
```

缓存 key = `sha256(provider|base_url|model_name|sha256(api_key))`（不存明文），LRU + TTL 30min + 上限 64 —— 替代现状 `agent.py` 里无界且只按模型名做 key 的 `dict`。
P0 同时修正 `DEEPSEEK_MODEL` 默认值为端点真实 id（`deepseek-flash`）。

### 7.5 能力降级（与 §6.1 联动）

| 差异 | 处理 |
| --- | --- |
| 不支持 function calling | `tools → pipeline` 降级，探测结果缓存 |
| 不支持流式 | 一次结果按 ~64 字符分片推 `delta`，前端无感 |
| 不返回 usage | 用 `tiktoken` 估算并标 `estimate=true` |
| 参数名不兼容 | 工厂按 provider 映射；提供 `extraBody` 逃生口 |
| 响应非标准 | 统一转 `error`（`type=model_error`），绝不回显上游原文 |

### 7.6 API

`GET|POST|PUT|DELETE /api/model-profiles`（列表只回掩码）· `POST /api/model-profiles/{id}/test` · `POST /api/model-profiles/test`（未保存前测试）· `GET /api/models` 保留（平台预置）。
`/api/chat` 请求新增 `modelConfig`（`provider`/`baseUrl`/`modelName`/`apiKey`/`timeoutSeconds`/`extraBody`）或 `modelProfileId`。

## 8. 阶段计划与验收

| 阶段 | 内容 | 验收（可度量） |
| --- | --- | --- |
| **前置** | OCRServer 缺陷修复（已完成，§10） | 见 §10 |
| **P0** | 重写为 `agent_service/` 分包；MySQL 仓储（只连自有域，会话域只读）；内部鉴权（`INTERNAL_AUTH_MODE=warn` 先上线）；日志脱敏；健康检查真实探活；幂等迁移器；修 `MILVUS_USER/PASSWORD` 未生效、默认模型名错误、异常回显 | 现有 curl 用例全过；前端无感知（8001 双端口验证）；`enforce` 下伪造 `userId` 被拒；日志/响应无密钥（自动化断言）；`<生产解释器> -c "import fastapi, langgraph, pymilvus, asyncmy"` 通过 |
| **P1** | 组件资产库 + 三档策略 + 计量 + 回填/校准脚本 | 回填抽取率 ≥ 历史生成消息的 70%；校准后同义召回 ≥ 80%、误复用 ≤ 2%；`reuse` 路径**不调用生成模型**（上游调用计数验证）；相对 legacy 基线 token 成本下降 ≥ 30%；Milvus 不可用时自动降级 FRESH |
| **P2** | `create_agent` + 5 工具 + 附件上下文 + 历史窗口裁剪 | 能完成"找类似组件 → 改深色"的工具链；不支持 tools 的模型自动降级；50 轮会话输入 token 不随轮数线性增长 |
| **P3** | 广场 API + 版本 + fork + 合规校验（+ 前端广场页） | 发布→广场可见→fork→二次修改→新版本闭环；§6.5 每条规则有测试；越权改他人组件被拒 |
| **P4** | 内联 `modelConfig` + 可选加密档 + SSRF + 工厂/缓存/降级 | 错误 Key 返回安全信息（无上游原文）；内网地址被拒且有测试（含 `::ffff:127.0.0.1`、`169.254.169.254`、重定向、非 https）；同模型名不同 Key 不串用；实例数有上限；日志零密钥 |
| **P5** | pytest（ASGI 直连）+ e2e 冒烟 + 文档/部署文件更新 | 四类测试全绿：SSE 契约、复用判定、鉴权、SSRF/脱敏 |

**推进顺序**：第 0 周三方冻结契约 → 第 1–2 周 P0 + 首次部署 → 第 3–4 周 P1（先 `GENERATION_POLICY=legacy` 只写不读攒数据，再切 `reuse`）→ 第 5 周 OCRServer 转发层 + P2 → 第 5–7 周 P3 与前端广场页并行 → 第 8 周 P4 与前端设置页并行。
**本地联调技巧**：旧版占 8000 保联调不断，新版跑 8001，把本地 `AGENT_SERVICE_URL` 临时指过去；首次上线复用同一招做蓝绿。

## 9. 依赖清单

| 依赖 | 状态 | 用途 | 必需性 |
| --- | --- | --- | --- |
| `langgraph` | 已装（传递） | Agent 运行时 | 建议显式声明 |
| `pydantic-settings`、`tiktoken` | 已装（传递） | 配置分层、token 估算 | 建议显式声明 |
| `asyncmy` | **缺** | MySQL 访问（C2 定稿） | **P0 必需** |
| `cryptography` | **缺** | 密钥加密落库（D2 选 B 时） | 条件必需 |
| `pytest`、`pytest-asyncio` | **缺** | 测试 | P5 必需 |
| `langgraph-checkpoint-postgres` | **缺** | 会话断点续跑 | 可选，首版不用 |
| `langchain-anthropic`、`langchain-google-genai` | **缺** | 非 OpenAI 兼容厂商 | 可选 |
| `tree-sitter`、`tree-sitter-typescript` | **缺** | 真 AST 解析 | 可选增强 |

安装前置：`unset all_proxy`（当前指向无监听端口，`uv add` 会失败）。

## 10. 已完成记录：OCRServer 侧缺陷修复（2026-10-09）

**问题**：`messages.content` 是 `TEXT`（65535 字节上限），而 OCRServer 把含 base64 图片的**原始消息**写进去；`sql_mode` 含 `STRICT_TRANS_TABLES` → `ER_DATA_TOO_LONG`，且异常发生在**生成之前**的写库阶段 → **整轮对话失败**（助手消息同理，超长代码会让生成后报错并丢历史）。

**证据**：DB 层 699,163 字节 INSERT → `1406`；HTTP 层真实 `POST /api/chat`（210,000 字节）→ `event: error {"error":"Data too long for column 'content'"}`。

**改动（4 文件，均在 `OCRServer/`）**：

| 文件 | 作用 |
| --- | --- |
| [migrations/001_messages_content_mediumtext.sql](../../OCRServer/migrations/001_messages_content_mediumtext.sql) | `content` → `MEDIUMTEXT`；含执行前/后核对与 DOWN 回滚 |
| [services/message_storage.js](../../OCRServer/services/message_storage.js) | 落库前剥离图片 base64，只留 `media_type` + 原始字节数（幂等） |
| [services/chat.js](../../OCRServer/services/chat.js) | `saveMessage` 改走 `toStorableContent()`（2 行，覆盖全部 4 条写库路径） |
| [scripts/cleanup_message_image_payloads.js](../../OCRServer/scripts/cleanup_message_image_payloads.js) | 清理历史遗留 base64 行（默认 dry-run，`--apply` 写库，幂等） |

**验证**：迁移执行后 `content=mediumtext`；同一 210,000 字节请求错误变为 `fetch failed`（下游 Python 未启动），消息成功落库；规范化函数 699,182 → 172 字节、幂等；清理脚本预演/执行/幂等三段通过；本地库基线 `conversations=1, messages=2` 复原（测试数据已清理）；`node --check` 通过且补丁后服务可正常启动。
**有意接受的副作用**：MiMo 直连模式下历史轮次截图不再回放给视觉模型（当轮图片仍实时可用）。恢复正确做法是对象存储 + 引用，而非把 base64 写回库。
**时区实证**：本机 `2026-10-09 03:05:40 +0800`；MySQL `NOW()` 经驱动返回 `2026-10-08T19:05:40.000Z`；`GET /api/history` 返回 `...Z` → 证实"列里存本地墙钟、接口输出 UTC"，是本服务必须显式转换的依据（§4.5）。

## 12. P0 实现记录（已完成，与设计的偏差一并记录）

### 12.1 实际落地的目录结构

```
agent_service/
  run.py                      # 真实入口（PM2 指向这里）
  main.py                     # 应用装配 + 配置校验 + 异常处理器（AppError → JSON；兜底 Exception → 500 安全描述）
  core/    config.py logging.py http.py security.py errors.py
  api/     deps.py sse.py routers/{system,chat,history,knowledge}.py
  schemas/ models.py
  services/ messages.py generation.py chat_service.py
  agent/   prompts.py llm/{factory,cache}.py
  repositories/ mysql.py sessions.py turns.py embeddings.py vector_store.py
  migrations/ 0001_components.sql
run.py                        # 兼容壳（转调 agent_service.run）
                              # 旧平铺实现已于 P0 验证通过后删除
deploy/milvus/                # 本地 Milvus compose 栈（Docker Desktop 里显示为项目 `milvus`）
  docker-compose.yml embedEtcd.yaml user.yaml
```

### 12.2 与设计的两处有意偏差

| 设计 | 实际实现 | 原因 |
| --- | --- | --- |
| `APP_ENV` 只加载 `.env.<APP_ENV>` | **分层加载**：`.env` 作基座 + `.env.<APP_ENV>` 叠加，且**进程环境变量优先级最高** | 避免把密钥复制一份到 `.env.local`；同时让 `AGENT_PORT=8001 uv run python run.py` 这类覆盖可用（蓝绿发布必需）。优先级：进程环境 > `.env.<APP_ENV>` > `.env` |
| 未提及出站代理 | 新增 `core/http.py`：默认**不信任环境代理**（`HTTP_TRUST_ENV=false`），可用 `MODEL_HTTP_PROXY` 显式指定 | 本机实测：环境里存在失效的 `all_proxy=socks5://127.0.0.1:7890`，httpx 在**构造客户端时**直接 `ImportError: Using SOCKS proxy...` —— 报错信息与真因完全无关，排查成本极高 |

### 12.3 错误码约定（已实测）

| 场景 | 状态码 | type |
| --- | --- | --- |
| 空 message / 未实现的可选字段（`modelConfig`/`attachments`，P0 明确拒绝） | 400 | `bad_request` |
| 缺少合法用户标识（非数字 userId、缺 `X-User-Id`） | 401 | `unauthorized` |
| 会话不存在 | 404 | `not_found` |
| **创建/删除会话**（归 OCRServer） | **501** | `session_domain_read_only` |
| 向量库不可用（Milvus 未起/配置错） | 503 | `vector_store_error` |
| 未预期异常 | 500 | `internal_error`（堆栈只进日志，已脱敏） |

### 12.4 路由的鉴权粒度

| 路由组 | 鉴权要求 |
| --- | --- |
| `/api/chat`、`/api/history*` | 需要用户身份（签名头优先，回落 `userId`；enforce 模式下不接受回落） |
| `/api/knowledge/*` | **只校验服务间签名，不要求用户身份**（运维/管理接口；旧的 curl 用法无 userId 也要能用） |
| `/`、`/health`、`/api/models` | 无需鉴权 |

### 12.5 实测验证清单（本机）

| 项 | 结果 |
| --- | --- |
| 配置守卫 | 本地误连远程 Milvus 被拒绝；`enforce` 无密钥被拒绝（启动即失败） |
| 迁移 | 首次应用 `0001_components.sql`，重复执行返回空（幂等），无 `already exists` 噪音 |
| 资产域表 | `components`/`component_*`/`conversation_turns`/`model_profiles` + `schema_migrations` 共 10 张全部就位 |
| 会话域只读 | 列表/详情与 OCRServer 输出**逐字节一致**（`2026-08-21T09:00:10.000Z`）；非数字 userId 被拒 |
| 会话写 | 三个入口（`DELETE/POST /api/history`、`DELETE /api/chat`）全部 501 |
| SSE 对话 | 真实生成跑通；`done` 含 `usage`（首次非 null）、`mode`、`reused`、`degraded`；Milvus 不可用时自动降级 |
| 回合元数据 | `conversation_turns` 正确写入（turn_index=1、mode、model） |
| 探活 | `/health` 真实探活 MySQL 双池（ok/ok）；`?deep=1` 真实调用嵌入模型（1024 维 ok）；Milvus 不可用时 `status=degraded` |
| 蓝绿 | `AGENT_PORT=8002 .venv/bin/python run.py` 生效（进程环境优先级验证） |

### 12.6 P0 未完成项

1. **知识库 `ingest`/`search` 的端到端验证尚未跑**（Milvus 阻塞已解除）。原先的「本地 Milvus standalone 起不来」已定位：根因**不是** macOS 文件共享层，而是宿主机 bind mount 上那份**陈旧的 etcd 元数据**（138MB）——同挂载方式下换全新 etcd，最慢 apply 从 **1197.9ms → 1.57ms**，8 秒 healthy；旧数据则触发 `etcdserver: leader changed`，在 `sessionutil.checkIDExist` 里 `panic(err)` 退出 134。处置：旧容器与 150MB 旧数据已删除，改由 [deploy/milvus/docker-compose.yml](../../deploy/milvus/docker-compose.yml) 提供；已实测 3 次冷启动（含 `down`+`up`）均 9 秒 healthy、数据保留、**0 致命 panic、0 `segcoreCode=2011`**。
2. **OCRServer 侧签名 header 未接**：本服务 `INTERNAL_AUTH_MODE` 目前为 `off`，待对方上线签名后切 `warn` → `enforce`。
3. 接口文档（README/CLAUDE.md）的接口细节待同步。


## 11. 风险与不做的事

| 风险 | 控制 |
| --- | --- |
| 误复用返回不合适组件 | 三重门槛 + 前端「换一个」+ `GENERATION_POLICY` 一键回退 legacy |
| 命中率低导致白做 | 历史回填 + 计量报表，把"重复需求占比"作为产品指标 |
| 组件库被灌水/抄袭 | 显式发布 + 静态校验 + 向量查重 + 举报 |
| 抽取准确率不足 | 首版接受误差但必须计量（抽取率/复用率/误复用率），据此决定是否上 tree-sitter |
| 弱模型污染组件库 | 入库静态校验 + 质量分 + 低质量不入 public |
| 向量库不可用 | 检索失败一律降级 FRESH 并记 `mode=fresh, degraded=true` |
| 换嵌入模型致全库失效 | 行上记录模型/维度，变更强制 reindex，禁止混用 |
| 环境不可验证 | P0 把健康检查做成真实探活；本机需先启动 Docker，再用 `docker compose -f deploy/milvus/docker-compose.yml up -d` 拉起 Milvus |
| 跨工程排期 | 只有 4 件事需同窗口：签名 header（可用 warn 模式解耦）、生产开关、时区约定、SSE 透传白名单 |

**不做**：不代改前端/OCRServer 业务代码（只给契约与清单）；不引入 PG checkpointer；不做真 AST（可选增强）；不做前端沙箱运行期隔离；不迁移会话真相源；不引入 Redis/MQ；不改平台级凭据来源（仅新增"用户自带"这一层）。
