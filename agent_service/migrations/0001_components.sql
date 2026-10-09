-- 0001: 本服务「资产域」建表（MySQL）
-- ─────────────────────────────────────────────────────────────
-- 域边界（C2）：本服务只拥有下列表；会话域（conversations/messages/users/works…）归 OCRServer。
-- 设计取舍：
--   * 不使用外键（应用层保证完整性）：避免与 OCRServer 的表产生跨域外键，也避免建表顺序问题。
--   * 时间列统一 DATETIME(3)：与 OCRServer 的既有列类型风格一致；写入约定见 DESIGN §4.5。
--   * 代码列用 MEDIUMTEXT：参照 OCRServer `works.source_code` 的既有做法，避免长组件被截断。
--   * 向量只存 Milvus，本表只存 ID 与元数据；本表是真相源，Milvus 索引可用脚本重建。

CREATE TABLE IF NOT EXISTS components (
  id                     CHAR(36)      NOT NULL COMMENT '应用层 uuid4()',
  owner_user_id          VARCHAR(64)   NOT NULL COMMENT 'users.id 的十进制字符串；禁止 anonymous',
  name                   VARCHAR(120)  NOT NULL DEFAULT '',
  kind                   VARCHAR(16)   NOT NULL COMMENT 'page|composite|atomic',
  category               VARCHAR(32)   NOT NULL DEFAULT 'other',
  description            TEXT          NOT NULL,
  language               VARCHAR(16)   NOT NULL DEFAULT 'tsx',
  code                   MEDIUMTEXT    NOT NULL,
  entry_wrapper          VARCHAR(32)   NOT NULL DEFAULT 'component',
  digest                 CHAR(64)      NOT NULL COMMENT '规范化指纹 sha256',
  semantic_text          VARCHAR(2048) NOT NULL DEFAULT '' COMMENT '被嵌入的检索串，查询侧同规则构造',
  embedding_model        VARCHAR(64)   NOT NULL DEFAULT '',
  embedding_dim          INT           NOT NULL DEFAULT 1024,
  visibility             VARCHAR(16)   NOT NULL DEFAULT 'private' COMMENT 'private|unlisted|public',
  status                 VARCHAR(16)   NOT NULL DEFAULT 'draft' COMMENT 'draft|published|removed|rejected',
  quality_score          FLOAT         NOT NULL DEFAULT 0,
  reuse_count            INT           NOT NULL DEFAULT 0,
  ref_count              INT           NOT NULL DEFAULT 0,
  like_count             INT           NOT NULL DEFAULT 0,
  fork_count             INT           NOT NULL DEFAULT 0,
  fork_of                CHAR(36)      NULL,
  source_conversation_id VARCHAR(64)   NULL,
  source_message_id      INT           NULL COMMENT 'messages.id 为 INT，保持一致',
  created_at             DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at             DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  published_at           DATETIME(3)   NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_components_owner_digest (owner_user_id, digest),
  KEY idx_components_owner (owner_user_id, created_at),
  KEY idx_components_gallery (visibility, status, published_at),
  KEY idx_components_category (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='组件资产真相源';

CREATE TABLE IF NOT EXISTS component_composition (
  parent_id CHAR(36)    NOT NULL,
  child_id  CHAR(36)    NOT NULL,
  slot      VARCHAR(64) NOT NULL DEFAULT '',
  PRIMARY KEY (parent_id, child_id, slot)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='组合关系：父组件由哪些原子组成';

CREATE TABLE IF NOT EXISTS component_versions (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  component_id CHAR(36)    NOT NULL,
  version      INT         NOT NULL,
  code         MEDIUMTEXT  NOT NULL,
  digest       CHAR(64)    NOT NULL,
  changelog    VARCHAR(500) NOT NULL DEFAULT '',
  created_by   VARCHAR(64) NOT NULL,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_component_version (component_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='版本链：迭代不覆盖历史';

CREATE TABLE IF NOT EXISTS component_likes (
  component_id CHAR(36)    NOT NULL,
  user_id      VARCHAR(64) NOT NULL,
  created_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (component_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS component_forks (
  id                  BIGINT      NOT NULL AUTO_INCREMENT,
  source_component_id CHAR(36)    NOT NULL,
  new_component_id    CHAR(36)    NOT NULL,
  user_id             VARCHAR(64) NOT NULL,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_forks_source (source_component_id),
  KEY idx_forks_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS component_reports (
  id               BIGINT       NOT NULL AUTO_INCREMENT,
  component_id     CHAR(36)     NOT NULL,
  reporter_user_id VARCHAR(64)  NOT NULL,
  reason           VARCHAR(500) NOT NULL DEFAULT '',
  status           VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending|accepted|rejected',
  handled_by       VARCHAR(64)  NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  handled_at       DATETIME(3)  NULL,
  PRIMARY KEY (id),
  KEY idx_reports_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS component_usage (
  id               BIGINT       NOT NULL AUTO_INCREMENT,
  component_id     CHAR(36)     NULL,
  conversation_id  VARCHAR(64)  NULL,
  user_id          VARCHAR(64)  NULL,
  mode             VARCHAR(16)  NOT NULL COMMENT 'reuse|reference|fresh|adjust',
  similarity       FLOAT        NULL,
  prompt_tokens    INT          NOT NULL DEFAULT 0,
  output_tokens    INT          NOT NULL DEFAULT 0,
  est_saved_tokens INT          NOT NULL DEFAULT 0,
  model            VARCHAR(128) NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_component_usage_time (created_at),
  KEY idx_component_usage_mode (mode, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='复用/生成计量账本';

CREATE TABLE IF NOT EXISTS conversation_turns (
  id              BIGINT      NOT NULL AUTO_INCREMENT,
  conversation_id VARCHAR(64) NOT NULL,
  turn_index      INT         NOT NULL,
  user_id         VARCHAR(64) NOT NULL,
  attachments     JSON        NOT NULL,
  tool_trace      JSON        NOT NULL,
  generation_mode VARCHAR(16) NOT NULL DEFAULT 'fresh',
  model           VARCHAR(128) NULL,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_turn (conversation_id, turn_index),
  KEY idx_turns_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='回合级元数据（附件/工具轨迹/生成模式）——不写 OCRServer 的 messages 表';

CREATE TABLE IF NOT EXISTS model_profiles (
  id             CHAR(36)     NOT NULL,
  user_id        VARCHAR(64)  NOT NULL,
  label          VARCHAR(64)  NOT NULL,
  provider       VARCHAR(32)  NOT NULL,
  base_url       VARCHAR(512) NOT NULL,
  model_name     VARCHAR(128) NOT NULL,
  api_key_cipher BLOB         NULL COMMENT '密文（Fernet）；明文永不入库',
  key_last4      VARCHAR(8)   NULL,
  extra          JSON         NULL,
  is_default     TINYINT      NOT NULL DEFAULT 0,
  created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uq_profile_label (user_id, label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户自带模型配置（D2 决定是否启用）';
