> 2026-10-26 运行时修订：共享执行图、动态模型路由、Tavily/记忆工具、Supabase PostgreSQL 消息/事件/检查点与长期记忆 Store 已接入；本文件中的 C3 决策保留为历史记录，当前边界见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。

# SnapUI Python 工程重构 · 计划与状态

> **本文件是活文档，只放状态 / 决策 / 下一步。** 设计细节、DDL、接口契约、阈值、验收标准在 [DESIGN.md](DESIGN.md)（按需查，不随每轮改动）。
> v1 全文（含当时的分析过程与实测原始记录）已归档：[archive/REFACTOR_PLAN_v1.md](archive/REFACTOR_PLAN_v1.md)。

## 1. 现状一句话

前端 + Node 后端（OCRServer，MySQL `OCRDB`）**已上线**；本服务（Python，FastAPI + DeepSeek + RAG）**未部署、可推翻重写**。本地：MySQL 原生可用（但只被 OCRServer 使用），Milvus 走 Docker（需先启动 Docker），当前已接入 Supabase PostgreSQL，保存消息、事件、检查点和跨会话偏好；MySQL 保留业务会话目录。以下 C2/C3 台账为历史决定，已被 2026-10-26 的存储修订覆盖。

## 2. 三仓库关系

| 仓库 | 状态 | 本方案的改动量 |
| --- | --- | --- |
| 前端 `my-tsx-react-app` | 已上线 | 小：埋点、广场页、模型设置页（P2–P4） |
| 后端 `OCRServer` | 已上线 | 小：转发路由、SSE 白名单透传、签名 header；本轮已修 2 处缺陷 |
| 本仓库 | 未部署 | **大：推翻重写** |

## 3. 决策台账

| # | 问题 | 结论 | 生效阶段 |
| --- | --- | --- | --- |
| S1 | 存储架构（原 C1/C2/A′ 三选一） | **C2：单库 MySQL，按表域分工；PG 退役** | P0 |
| S2 | Milvus 双环境（本地 standalone / 生产 Zilliz Cloud） | **已定策略**：`APP_ENV` 分环境 + `MILVUS_MODE` 自动/显式 + 云端不建库只校验 + 防误连闸门；已实测云端支持 schema 建集合与 `INVERTED`、不 flush 也可检索 | P0 |
| D3 | 会话真相源 | 会话域（`conversations`/`messages`）归 OCRServer；**C3 后组件资产域也归它**，本服务不再拥有任何关系表 | P0 → C3 |
| D9 | 已有的 `works` 表 | **复用为「广场投影表」**，另建 `components` 作资产真相源 | P1/P3 |
| D4 | 广场首版范围 | MVP：发布 / 列表 / 详情 / fork / 版本；点赞举报后置 | P3 |
| D7 | PM2 部署 | 已改完：`.venv` 解释器 + 根 `run.py` 入口 + 三个可覆盖常量 | 已完成 |
| D8 | 积分规则 | 由 OCRServer 决定；本服务只提供 usage 数据 | — |
| **D1** | Agent 框架：`create_agent` vs 手写循环 | **待定**（建议 `create_agent`） | 必须在 P2 前 |
| **D2** | 用户密钥：仅内联传参 vs 加密落库 | **已定：OCRServer 加密落库，聊天使用 modelProfileId；前端不持久化密钥** | 必须在 P4 前 |
| **D5** | 复用档位：保守 vs 激进 | **待定**（建议保守） | 必须在 P1 前 |
| **D6** | 是否引入 pytest | **待定**（建议引入） | 必须在 P5 前 |
| D10 | PG 遗留怎么处理（[docker-compose.yml](../../docker-compose.yml) 的 `postgres` 服务 + `POSTGRES_DSN`、本机 `postgre` 容器） | **已处置**（2026-10-09 13:0x，按下述方案清理）：compose 删除 `postgres` 服务、`pgdata` 卷、`POSTGRES_DSN` 与 `depends_on` 门槛，只保留 `app`，改为透传 `MYSQL_DSN`/`MYSQL_RO_DSN`（容器内主机名不同，可用 `MYSQL_DSN_DOCKER`/`MYSQL_RO_DSN_DOCKER` 覆盖）+ `extra_hosts: host.docker.internal:host-gateway`，并补上缺了就必然起不来的 `APP_ENV`（默认 `production`；`local` 档会拒绝连远程 Milvus）。验证：`docker compose config` exit 0、服务仅 `app`、嵌套覆盖 `${MYSQL_DSN_DOCKER:-${MYSQL_DSN}}` 实测生效。本机 `postgre` 容器（postgres:16）与其匿名卷 `6dc20fb2a68be7…` 已删除（删除前核对：仅该容器挂载该卷；`milvus-standalone` 未受影响）| 已完成 |
| **C3** | 关系库归属（**修订 C2 / D3**） | **OCRServer 独占关系库，本服务零 SQL**：① 历史由 Node 在 `/api/chat` 请求体 `history` 里下发（ISO-8601 UTC，本服务不再按 `LEGACY_DB_TIMEZONE` 解释墙钟）；② 组件资产域 DDL 迁到 `OCRServer/migrations/002_agent_domain.sql`，回合元数据由 Node 从 `done` 帧落库；③ 本服务删除 `mysql.py`/`sessions.py`/`turns.py`/`migrations/`/`/api/history` 与 `asyncmy`+`psycopg` 依赖；④ Milvus 仍留在本服务（向量检索需与嵌入模型同址，且不是 SQL） | **已完成（2026-10-09 13:4x，已端到端实测）** |

## 4. 不可违背的约束

1. **SSE 只增字段**：`start`/`delta`/`done`/`error` 与现有 8 组接口路径、参数全部保持兼容。
2. **`messages` 是原始真相，`components` 是派生索引**；禁止让消息反向引用组件（见 DESIGN「存储与域边界」）。
3. **当前存储边界**：Python 只连接 PostgreSQL 和 Milvus；OCRServer 只连接业务 MySQL，消息通过 Python 内部接口读写。历史 C3 的零 SQL 约束已取消。
4. **密钥不进日志、不进响应、不进 Milvus**；用户可控 `baseUrl` 必须过 SSRF 校验。
5. **`user_id` = `users.id` 的十进制字符串**（OCRServer 鉴权后下发）；时间一律由上游以 **ISO-8601 UTC** 给出，本服务不解释本地墙钟（原 `LEGACY_DB_TIMEZONE` 契约随 C3 取消）。
6. **包名 `agent_service/`**（PM2 期望的路径），根 `run.py` 保留兼容壳。

## 5. 进度看板

| 阶段 | 内容 | 状态 | 完成判据 |
| --- | --- | --- | --- |
| 前置 | OCRServer 侧缺陷修复（`content`→`MEDIUMTEXT`、图片剥离 base64） | ✅ 代码与本机库已完成；**生产待执行** | 见 §8 变更日志与 DESIGN「已完成记录」 |
| P0 | 地基：重写分包 + 内部鉴权 + 脱敏 + 探活（**MySQL 仓储部分已于 C3 摘除**） | **代码完成并已实测**；剩 2 项待收口（见 §5 备注） | DESIGN §12「P0 实现记录」 |
| P1 | 组件原子化 + 向量复用（需求①） | 未开始 | 同上 |
| P2 | Agent 化 + 附件上下文 | 未开始 | 同上 |
| P3 | 作品广场（需求②后半） | 未开始 | 同上 |
| P4 | 用户自定义模型（需求③） | 提前实现模型配置管理与调用；高级参数/能力降级后置，验收进行中 | 同上 |
| P5 | 测试与验收收口 | 未开始 | 同上 |

### P0 收口备注（已完成 / 待办）

**已完成并实测**：`agent_service/` 分包重写；内部鉴权（HMAC，off/warn/enforce 三模式，本机仍待接）；日志脱敏；真实探活（`/health` + `?deep=1`，C3 后只探 Milvus 与嵌入模型）；默认模型名修正；`MILVUS_USER/PASSWORD` 补上；异常不再回显原文；出站代理策略；配置优先级（进程环境 > .env.<APP_ENV> > .env）；根 `run.py` 切换到新实现、旧平铺代码已删除。**C3 摘除关系库**：MySQL 双池 + 迁移器 + 会话域只读仓储 + 回合写入全部删除，`/api/history` 整组下线（Node 本就有同路径原生实现），`asyncmy`/`psycopg*` 依赖移除。

**待办**：
1. 知识库 `ingest`/`search` 端到端验证（`search` 已实测 200；`ingest` 写路径尚未跑）。本地 Milvus 阻塞已解除：根因是宿主机 bind mount 上陈旧的 etcd 元数据（138MB）而非文件共享层（A/B：同挂载方式换全新 etcd，最慢 apply 1197.9ms → 1.57ms），旧容器与 150MB 旧数据已删，改由 `deploy/milvus/` compose 栈提供（3 次冷启动均 9 秒 healthy）。
2. OCRServer 侧加签名 header（`X-User-Id`/`X-Internal-Timestamp`/`X-Internal-Signature`）后，本服务把 `INTERNAL_AUTH_MODE` 从 `off` 切到 `warn` 再切 `enforce`。
3. README/CLAUDE.md/AGENT.md 的接口说明同步（C3 后端点从 11 个降到 7 个，已同步）。
4. ~~compose 缺 `MYSQL_DSN`~~ **已作废**：C3 后本服务不再需要任何数据库环境变量，compose 里残留的 `MYSQL_DSN`/`MYSQL_RO_DSN`/`LEGACY_DB_TIMEZONE` 三行已随之删除。

## 6. 等你（人类）做的事

1. **最高优先级**：服务器上确认 `grep AGENT_SERVICE_ENABLED .env.production`。仓库内该值为 `true` 而生产未部署本服务——若服务器上同为 `true`，**线上聊天此刻是坏的**（每次请求都连不上 `127.0.0.1:8000`）。
2. 生产库执行 `OCRServer/migrations/001_messages_content_mediumtext.sql`（先看行数与体积；`TEXT→MEDIUMTEXT` 是表重建，可能锁表）。
3. 生产库执行 `node OCRServer/scripts/cleanup_message_image_payloads.js`（先 dry-run，再加 `--apply`）。
4. **轮换 Milvus Token 并清理模板**：`.env.example` 里的 `MILVUS_TOKEN` 与 `.env` **是同一个值**（指纹相同），而 `.env.example` 是要进版本库的 → 该 Token 视同已泄露；请到 Zilliz 控制台重置，`.env.example` 只留 `MILVUS_TOKEN=xxx` 占位。
5. **拆环境配置文件**：现在唯一的 `.env` 指向云端，而本地开发要连 standalone → 拆成 `.env.local`（standalone，无 token）与生产 `.env`（云端 + token），P0 的 `APP_ENV` 负责选择。
6. 定 D1 / D2 / D5 / D6。

## 7. 下一步

**P0 已收口，下一步是 P1（组件原子化 + 向量复用）**，但开工前必须先定 **D5（复用档位：保守 vs 激进）**。落地顺序：组件资产表 → 抽取/入库 → 三档复用策略（`T_REUSE`/`T_REF`/`T_DUP`，阈值需 ≥100 组标注对校准）→ 计量（抽取率/复用率/误复用率）。

## 8. 变更日志（只追加，最新在上）

- **2026-10-09** 提示词集中到 OCRServer/services/chatPrompts.js。前端只提交原始文字/图片、显式 new/adjust、引用作品 ID 及会话/模型选择；不上传 system、引用源码或来源标题。Node 校验作品可见性并读取源码；普通调整使用该会话最近助手组件。组装上下文只用于本轮模型调用，消息历史保存原始用户输入。按钮与计费不变，未引入意图推测或 Subagent，Python 仍零 SQL。


- **2026-10-09** 用户确定 D2 采用后端加密配置档；本轮按用户需求提前推进 P4，不宣称 P1/P2/P3 完成。OCRServer 复用 model_profiles 表，负责 CRUD/归属校验与 AES-256-GCM 加密（替代旧 Fernet 草案，避免给零 SQL Python 引入加密存储依赖）；Python 只收本轮解密参数，校验内部签名、使用公共 IP 固定连接的 HTTPS 出站客户端，不写 SQL。前端模型管理在 /profile，聊天只传 modelProfileId。主密钥为 MODEL_PROFILE_ENCRYPTION_KEY（32 字节 base64），未配置时存储返回 503。高级参数、自动降级、测试连接接口仍后置。


- **2026-10-09 13:4x** **C3 落地并端到端实测：本服务零 SQL。** Python 侧删除 `repositories/mysql.py`（双池 + 迁移器）、`sessions.py`、`turns.py`、`migrations/`（9 张自有域表 DDL）、`api/routers/history.py` 与 `DELETE /api/chat/:id`，`SessionDomainReadOnly`(501) 一并移除；`config.py` 去掉 `MYSQL_DSN`/`MYSQL_RO_DSN`/`MYSQL_POOL_*`/`LEGACY_DB_TIMEZONE`，`/health` 改为只报 `storage: none` + Milvus + 嵌入模型；`ChatRequest` 新增 `history`（最近 20 条参与组装）；依赖移除 `asyncmy`/`psycopg`/`psycopg-pool`（`uv remove` 顺带卸掉了 venv 里的 `pip`，venv 由 uv 管理不受影响）。OCRServer 侧：`services/chat.js` 新增 `loadRecentHistory()` 与 `recordAgentTurn()`，**先读本轮之前的历史、再写本轮用户消息**（顺带消掉原先「Node 先写库、Python 再用另一条连接读回来」的一致性隐患），请求体带上 `history`，并从 `done` 帧取 `mode` 落 `conversation_turns`；资产域 DDL 迁入 [OCRServer/migrations/002_agent_domain.sql](../../OCRServer/migrations/002_agent_domain.sql)（执行两次验证幂等，9 张表齐备）。**实测**：真链路 Node→Python 跑一轮，模型从只在 MySQL 历史里的暗号答出「紫色河马」（证明 history 生效）；会话内 4 条消息正常落库；`conversation_turns` 写入 1 行（turn_index=1 / fresh / deepseek-flash）；`/health?deep=1` = `ok`（milvus ok、嵌入 1024 维 ok）；测试数据已复原（conv=1 / msg=2 / turns=0，顺带清掉 P0 遗留的孤儿回合行）。

- **2026-10-09 13:0x** 按 D10 清理 PG 遗留：根 [docker-compose.yml](../../docker-compose.yml) 删除 `postgres` 服务 / `pgdata` 卷 / `POSTGRES_DSN` / `depends_on` 门槛，改为只跑 `app`，透传 `MYSQL_DSN` + `MYSQL_RO_DSN`（新增 `MYSQL_DSN_DOCKER`/`MYSQL_RO_DSN_DOCKER` 用于覆盖容器内主机名）、加 `extra_hosts: host.docker.internal:host-gateway`，并补上原本缺失、缺了必然起不来的 `APP_ENV`（默认 `production`）与 `INTERNAL_AUTH_*`。验证：`docker compose config` **exit 0**、服务仅 `app`、嵌套覆盖 `${MYSQL_DSN_DOCKER:-${MYSQL_DSN}}` 实测生效（有覆盖用覆盖、无覆盖用 `.env`）。附带实测宿主机连通性（Docker Desktop 29.8.2）：本机 MySQL 8.4.9 确实监听了非 loopback 地址（从宿主机连 LAN IP `192.168.31.117:3306` 被回 **1130 Host ... is not allowed**），但**容器内 `host.docker.internal:3306` 超时不通，改用宿主机 LAN IP `:3306` 可建立 TCP**；对照组「容器连宿主机 LAN IP 上由 Docker 发布的 `:19530`」同样超时（hairpin，属正常）→ 结论：容器部署的 MySQL 主机名必须按目标机实测填、账号还须授权容器来源，已写进 compose 注释。README / AGENT.md / `.env.example` 里的 PG 表述同步改掉；本机 `postgre` 容器与其匿名卷 `6dc20fb2a68be7…` 经人工确认后删除（删前核对：仅该容器挂载该卷，`milvus-standalone` 未受影响）。**未做**：容器镜像构建 + 真实启动验证（人工选择只做静态校验），因此"compose 能起来"目前只是配置层面的结论。
- **2026-10-09 12:2x** 本地 Milvus 链路重做：定位「起不来」的根因是宿主机 bind mount 上**陈旧的 etcd 元数据**（138MB）而非 macOS 文件共享层（A/B 对照：同挂载方式换全新 etcd，最慢 apply **1197.9ms → 1.57ms**）；删除旧容器与 150MB 旧数据，新增 `deploy/milvus/` compose 栈（镜像 `milvusdb/milvus:v3.0.0` + 命名卷 `milvus-data`，Docker Desktop 显示为项目 `milvus`、可 GUI 启停），3 次冷启动含 `down`+`up` 均 9 秒 healthy、数据保留、0 致命 panic。顺带查实：旧 `user.yaml` 里的 `minioConfig.rootPath` 是**无效键**（镜像内命中 0 次，正确路径为 `minio.rootPath`），从未生效，疑似 2011 的归因有误。
- **2026-10-09 04:0x** Milvus 双环境策略定稿（S2）并实测云端：可连、`INVERTED` 支持、schema 建集合成功、不 flush 亦可检索；同时发现 ① 云端只有一个库 `db_b15abbec17725da`，`.env` 的 `MILVUS_DB=snapui_rag` 在云端不存在（会被静默忽略）；② 云端集合为空，生产 RAG 知识库尚未建立；③ `.env` 当前指向云端（本地开发有误写生产集群的风险）；④ `.env.example` 与 `.env` 的 Token 相同。
- **2026-10-09 11:5x** P0 落地并实测：重写为 `agent_service/` 分包（16 个模块）；MySQL 双池 + 迁移器（资产域 9 表已建）；SSE 对话跑通（`usage` 首次真实返回、`done` 新增 mode/reused/degraded）；历史只读接口与 OCRServer 输出**逐字节一致**（`2026-08-21T09:00:10.000Z`）；会话写操作三入口全部 501；错误码 400/401/404/501/503 全部按设计。顺带修掉：默认模型名、`MILVUS_USER/PASSWORD` 失效、异常回显、`sessions` 模块/实例同名 500、向量库初始化未包装成 503、环境变量被 `.env` 覆盖导致无法蓝绿换端口、出站代理导致 httpx 构造期 ImportError。旧平铺模块（8 个文件）在验证通过后已删除。
- **2026-10-09 03:4x** 文档瘦身：v1（1179 行）归档，拆为「计划与状态」（本文件）+「设计」[DESIGN.md](DESIGN.md)。
- **2026-10-09 03:0x** OCRServer 侧修复完成并实测：迁移文件 + 图片剥离模块 + 清理脚本（共 4 文件）；本机库已执行迁移；生产待执行。附带拿到时区实证（DATETIME 存本地墙钟、接口输出 UTC，相差 8 小时）。
- **2026-10-09 02:5x** 环境核实：MySQL 原生可用（8.4.9，`OCRDB`，7 张表）；发现无人使用的 `works`/`orders` 表；`messages.content` 为 `TEXT`（风险确认）、`user_id` 为 `INT`；Docker 守护进程未运行（PG/Milvus 容器均为停止态）；认证代理 `all_proxy` 指向无监听端口。
- **2026-10-09 02:2x** PM2 配置修正：[ecosystem.config.cjs](../../OCRServer/ecosystem.config.cjs) 改为 uv `.venv` 解释器 + 根 `run.py` 入口，目录名/解释器/入口三项可环境变量覆盖。
- **2026-10-09** C2 定稿（PG 退役，单库 MySQL 表域分工）；D9 定（`works` 作广场投影 + 新建 `components`）；重写获授权（Python 未上线，无兼容包袱）。
