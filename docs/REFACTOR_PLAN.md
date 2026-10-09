# SnapUI Python 工程重构 · 计划与状态

> **本文件是活文档，只放状态 / 决策 / 下一步。** 设计细节、DDL、接口契约、阈值、验收标准在 [DESIGN.md](DESIGN.md)（按需查，不随每轮改动）。
> v1 全文（含当时的分析过程与实测原始记录）已归档：[archive/REFACTOR_PLAN_v1.md](archive/REFACTOR_PLAN_v1.md)。

## 1. 现状一句话

前端 + Node 后端（OCRServer，MySQL `OCRDB`）**已上线**；本服务（Python，FastAPI + DeepSeek + RAG）**未部署、可推翻重写**。本地：MySQL 原生可用，Milvus 走 Docker（需先启动 Docker），PostgreSQL 随 C2 决策退役。

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
| D3 | 会话真相源 | 会话域（`conversations`/`messages`）归 OCRServer，本服务**只读**；组件资产域归本服务 | P0 |
| D9 | 已有的 `works` 表 | **复用为「广场投影表」**，另建 `components` 作资产真相源 | P1/P3 |
| D4 | 广场首版范围 | MVP：发布 / 列表 / 详情 / fork / 版本；点赞举报后置 | P3 |
| D7 | PM2 部署 | 已改完：`.venv` 解释器 + 根 `run.py` 入口 + 三个可覆盖常量 | 已完成 |
| D8 | 积分规则 | 由 OCRServer 决定；本服务只提供 usage 数据 | — |
| **D1** | Agent 框架：`create_agent` vs 手写循环 | **待定**（建议 `create_agent`） | 必须在 P2 前 |
| **D2** | 用户密钥：仅内联传参 vs 加密落库 | **待定**（建议先内联） | 必须在 P4 前 |
| **D5** | 复用档位：保守 vs 激进 | **待定**（建议保守） | 必须在 P1 前 |
| **D6** | 是否引入 pytest | **待定**（建议引入） | 必须在 P5 前 |
| D10 | PG 遗留怎么处理（[docker-compose.yml](../../docker-compose.yml) 的 `postgres` 服务 + `POSTGRES_DSN`、本机 `postgre` 容器） | **已处置**（2026-10-09 13:0x，按下述方案清理）：compose 删除 `postgres` 服务、`pgdata` 卷、`POSTGRES_DSN` 与 `depends_on` 门槛，只保留 `app`，改为透传 `MYSQL_DSN`/`MYSQL_RO_DSN`（容器内主机名不同，可用 `MYSQL_DSN_DOCKER`/`MYSQL_RO_DSN_DOCKER` 覆盖）+ `extra_hosts: host.docker.internal:host-gateway`，并补上缺了就必然起不来的 `APP_ENV`（默认 `production`；`local` 档会拒绝连远程 Milvus）。验证：`docker compose config` exit 0、服务仅 `app`、嵌套覆盖 `${MYSQL_DSN_DOCKER:-${MYSQL_DSN}}` 实测生效。本机 `postgre` 容器（postgres:16）与其匿名卷 `6dc20fb2a68be7…` 已删除（删除前核对：仅该容器挂载该卷；`milvus-standalone` 未受影响）| 已完成 |

## 4. 不可违背的约束

1. **SSE 只增字段**：`start`/`delta`/`done`/`error` 与现有 8 组接口路径、参数全部保持兼容。
2. **`messages` 是原始真相，`components` 是派生索引**；禁止让消息反向引用组件（见 DESIGN「存储与域边界」）。
3. **不跨域写、不建跨服务外键、不做跨服务 JOIN**：本服务永不写 `users`/`conversations`/`messages`。
4. **密钥不进日志、不进响应、不进 Milvus**；用户可控 `baseUrl` 必须过 SSRF 校验。
5. **`user_id` = `users.id` 的十进制字符串**；读 OCRServer 的 DATETIME 必须按 `LEGACY_DB_TIMEZONE`（默认 `Asia/Shanghai`）解释再转 UTC。
6. **包名 `agent_service/`**（PM2 期望的路径），根 `run.py` 保留兼容壳。

## 5. 进度看板

| 阶段 | 内容 | 状态 | 完成判据 |
| --- | --- | --- | --- |
| 前置 | OCRServer 侧缺陷修复（`content`→`MEDIUMTEXT`、图片剥离 base64） | ✅ 代码与本机库已完成；**生产待执行** | 见 §8 变更日志与 DESIGN「已完成记录」 |
| P0 | 地基：重写分包 + MySQL 仓储 + 内部鉴权 + 脱敏 + 探活 + 迁移器 | **代码完成并已实测**；剩 2 项待收口（见 §5 备注） | DESIGN §12「P0 实现记录」 |
| P1 | 组件原子化 + 向量复用（需求①） | 未开始 | 同上 |
| P2 | Agent 化 + 附件上下文 | 未开始 | 同上 |
| P3 | 作品广场（需求②后半） | 未开始 | 同上 |
| P4 | 用户自定义模型（需求③） | 未开始 | 同上 |
| P5 | 测试与验收收口 | 未开始 | 同上 |

### P0 收口备注（已完成 / 待办）

**已完成并实测**：`agent_service/` 分包重写；MySQL 双池（自有域读写 / 会话域只读）；幂等迁移器 + 资产域 9 张表；内部鉴权（HMAC，off/warn/enforce 三模式，本机 warn 待接）；日志脱敏；真实探活（`/health` + `?deep=1`）；默认模型名修正；`MILVUS_USER/PASSWORD` 补上；异常不再回显原文；出站代理策略；配置优先级（进程环境 > .env.<APP_ENV> > .env）；根 `run.py` 切换到新实现、旧平铺代码已删除。

**待办**：
1. 知识库 `ingest`/`search` 端到端验证。阻塞已解除：本地 Milvus 的根因是宿主机 bind mount 上陈旧的 etcd 元数据（138MB）而非文件共享层（A/B：同挂载方式换全新 etcd，最慢 apply 1197.9ms → 1.57ms），旧容器与 150MB 旧数据已删，改由 `deploy/milvus/` compose 栈提供（3 次冷启动均 9 秒 healthy）。
2. OCRServer 侧加签名 header（`X-User-Id`/`X-Internal-Timestamp`/`X-Internal-Signature`）后，本服务把 `INTERNAL_AUTH_MODE` 从 `off` 切到 `warn` 再切 `enforce`。
3. README/CLAUDE.md 的接口说明同步（结构已同步，接口细节待补）。
4. ~~[docker-compose.yml](../../docker-compose.yml) 缺 `MYSQL_DSN`~~ **已完成（2026-10-09 13:0x）**：按 D10 清理，compose 现在只跑 `app` 并透传 `MYSQL_DSN`，`depends_on: postgres` 已移除。

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

- **2026-10-09 13:0x** 按 D10 清理 PG 遗留：根 [docker-compose.yml](../../docker-compose.yml) 删除 `postgres` 服务 / `pgdata` 卷 / `POSTGRES_DSN` / `depends_on` 门槛，改为只跑 `app`，透传 `MYSQL_DSN` + `MYSQL_RO_DSN`（新增 `MYSQL_DSN_DOCKER`/`MYSQL_RO_DSN_DOCKER` 用于覆盖容器内主机名）、加 `extra_hosts: host.docker.internal:host-gateway`，并补上原本缺失、缺了必然起不来的 `APP_ENV`（默认 `production`）与 `INTERNAL_AUTH_*`。验证：`docker compose config` **exit 0**、服务仅 `app`、嵌套覆盖 `${MYSQL_DSN_DOCKER:-${MYSQL_DSN}}` 实测生效（有覆盖用覆盖、无覆盖用 `.env`）。附带实测宿主机连通性（Docker Desktop 29.8.2）：本机 MySQL 8.4.9 确实监听了非 loopback 地址（从宿主机连 LAN IP `192.168.31.117:3306` 被回 **1130 Host ... is not allowed**），但**容器内 `host.docker.internal:3306` 超时不通，改用宿主机 LAN IP `:3306` 可建立 TCP**；对照组「容器连宿主机 LAN IP 上由 Docker 发布的 `:19530`」同样超时（hairpin，属正常）→ 结论：容器部署的 MySQL 主机名必须按目标机实测填、账号还须授权容器来源，已写进 compose 注释。README / AGENT.md / `.env.example` 里的 PG 表述同步改掉；本机 `postgre` 容器与其匿名卷 `6dc20fb2a68be7…` 经人工确认后删除（删前核对：仅该容器挂载该卷，`milvus-standalone` 未受影响）。**未做**：容器镜像构建 + 真实启动验证（人工选择只做静态校验），因此"compose 能起来"目前只是配置层面的结论。
- **2026-10-09 12:2x** 本地 Milvus 链路重做：定位「起不来」的根因是宿主机 bind mount 上**陈旧的 etcd 元数据**（138MB）而非 macOS 文件共享层（A/B 对照：同挂载方式换全新 etcd，最慢 apply **1197.9ms → 1.57ms**）；删除旧容器与 150MB 旧数据，新增 `deploy/milvus/` compose 栈（镜像 `milvusdb/milvus:v3.0.0` + 命名卷 `milvus-data`，Docker Desktop 显示为项目 `milvus`、可 GUI 启停），3 次冷启动含 `down`+`up` 均 9 秒 healthy、数据保留、0 致命 panic。顺带查实：旧 `user.yaml` 里的 `minioConfig.rootPath` 是**无效键**（镜像内命中 0 次，正确路径为 `minio.rootPath`），从未生效，疑似 2011 的归因有误。
- **2026-10-09 04:0x** Milvus 双环境策略定稿（S2）并实测云端：可连、`INVERTED` 支持、schema 建集合成功、不 flush 亦可检索；同时发现 ① 云端只有一个库 `db_b15abbec17725da`，`.env` 的 `MILVUS_DB=snapui_rag` 在云端不存在（会被静默忽略）；② 云端集合为空，生产 RAG 知识库尚未建立；③ `.env` 当前指向云端（本地开发有误写生产集群的风险）；④ `.env.example` 与 `.env` 的 Token 相同。
- **2026-10-09 11:5x** P0 落地并实测：重写为 `agent_service/` 分包（16 个模块）；MySQL 双池 + 迁移器（资产域 9 表已建）；SSE 对话跑通（`usage` 首次真实返回、`done` 新增 mode/reused/degraded）；历史只读接口与 OCRServer 输出**逐字节一致**（`2026-08-21T09:00:10.000Z`）；会话写操作三入口全部 501；错误码 400/401/404/501/503 全部按设计。顺带修掉：默认模型名、`MILVUS_USER/PASSWORD` 失效、异常回显、`sessions` 模块/实例同名 500、向量库初始化未包装成 503、环境变量被 `.env` 覆盖导致无法蓝绿换端口、出站代理导致 httpx 构造期 ImportError。旧平铺模块（8 个文件）在验证通过后已删除。
- **2026-10-09 03:4x** 文档瘦身：v1（1179 行）归档，拆为「计划与状态」（本文件）+「设计」[DESIGN.md](DESIGN.md)。
- **2026-10-09 03:0x** OCRServer 侧修复完成并实测：迁移文件 + 图片剥离模块 + 清理脚本（共 4 文件）；本机库已执行迁移；生产待执行。附带拿到时区实证（DATETIME 存本地墙钟、接口输出 UTC，相差 8 小时）。
- **2026-10-09 02:5x** 环境核实：MySQL 原生可用（8.4.9，`OCRDB`，7 张表）；发现无人使用的 `works`/`orders` 表；`messages.content` 为 `TEXT`（风险确认）、`user_id` 为 `INT`；Docker 守护进程未运行（PG/Milvus 容器均为停止态）；认证代理 `all_proxy` 指向无监听端口。
- **2026-10-09 02:2x** PM2 配置修正：[ecosystem.config.cjs](../../OCRServer/ecosystem.config.cjs) 改为 uv `.venv` 解释器 + 根 `run.py` 入口，目录名/解释器/入口三项可环境变量覆盖。
- **2026-10-09** C2 定稿（PG 退役，单库 MySQL 表域分工）；D9 定（`works` 作广场投影 + 新建 `components`）；重写获授权（Python 未上线，无兼容包袱）。
