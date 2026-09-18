-- 001_init.sql —— 初始 schema
-- 注意：硬删除（ADR-7），posts 表无 deleted_at 列。

CREATE TABLE posts (
  tid           INTEGER PRIMARY KEY,       -- 论坛帖子 ID
  title         TEXT    NOT NULL DEFAULT '',
  code          TEXT,                      -- 番号，如 START-624
  actress       TEXT,
  release_date  TEXT,                      -- 商品発売日 YYYY-MM-DD
  size          TEXT,                      -- 如 7GB
  cover_img     TEXT,                      -- 封面图源 URL（API 中映射为 cover）
  detail_img    TEXT,                      -- 详情图源 URL（API 中映射为 detail）
  ed2k          TEXT,                      -- NULL = 未解锁或无链接
  post_date     TEXT    NOT NULL,          -- 归档依据 YYYY-MM-DD (UTC+8)
  post_time     TEXT,                      -- 完整时间戳 RFC3339 (UTC+8)
  status        TEXT    NOT NULL,          -- pending|thanked|done|failed|nolink
  retry_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  next_retry_at TEXT,
  emby_status   TEXT,                      -- none|in_library（缓存）
  emby_item_id  TEXT,
  emby_checked  TEXT,
  tg_sent_at    TEXT,                      -- NULL = 未转发
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL
);

CREATE INDEX idx_browse  ON posts(post_date)     WHERE status='done';
CREATE INDEX idx_pending ON posts(next_retry_at) WHERE status IN ('pending','failed','thanked');
CREATE INDEX idx_code    ON posts(code)          WHERE code IS NOT NULL;
CREATE INDEX idx_emby    ON posts(emby_checked);
CREATE INDEX idx_tg      ON posts(tg_sent_at)    WHERE tg_sent_at IS NULL;

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  encrypted  INTEGER NOT NULL DEFAULT 0,   -- 1 = value 为密文
  updated_at TEXT NOT NULL
);

CREATE TABLE forum_session (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  cookies             TEXT    NOT NULL,    -- JSON 序列化 cookie 列表
  username            TEXT,
  obtained_at         TEXT    NOT NULL,
  expires_at          TEXT,                -- 预计失效（30 天）
  valid               INTEGER NOT NULL DEFAULT 1,
  login_attempts_left INTEGER,
  last_login_attempt  TEXT,
  last_relogin_at     TEXT,                -- 上次自动重登录时间
  relogin_state       TEXT                 -- ok|retrying|failed|blocked
);

CREATE TABLE panel_session (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
