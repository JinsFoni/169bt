-- 002_collect.sql —— 采集任务表
--
-- 采集是长任务（数百帖 × 2-5 秒限速 = 数十分钟），必须能：
--   1. 后台跑（不能阻塞 HTTP 请求）
--   2. 中途查询进度（前端轮询）
--   3. 取消
--   4. 进程重启后仍能看到「上次跑到哪」
--
-- 只保留一条活跃任务（个人自用、并发度恒为 1），但历史记录保留供排查。

CREATE TABLE collect_jobs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  from_date    TEXT    NOT NULL,          -- YYYY-MM-DD（含）
  to_date      TEXT    NOT NULL,          -- YYYY-MM-DD（含）
  fid          TEXT    NOT NULL DEFAULT '192',
  status       TEXT    NOT NULL,          -- running|done|failed|cancelled
  phase        TEXT    NOT NULL DEFAULT 'listing',  -- listing|fetching|done
  total        INTEGER NOT NULL DEFAULT 0,          -- 发现的帖子数
  processed    INTEGER NOT NULL DEFAULT 0,          -- 已处理（含跳过）
  collected    INTEGER NOT NULL DEFAULT 0,          -- 新增入库
  skipped      INTEGER NOT NULL DEFAULT 0,          -- 已存在而跳过
  failed       INTEGER NOT NULL DEFAULT 0,          -- 处理失败
  pages        INTEGER NOT NULL DEFAULT 0,          -- 已翻页数
  current_tid  INTEGER,                             -- 正在处理的 tid
  message      TEXT,                                -- 人类可读状态/错误
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  started_at   TEXT    NOT NULL,
  finished_at  TEXT
);

-- 同一时刻只允许一个未完成任务
CREATE UNIQUE INDEX idx_collect_active
  ON collect_jobs(status) WHERE status = 'running';

CREATE INDEX idx_collect_recent ON collect_jobs(started_at DESC);
