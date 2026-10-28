-- 007_ltx.sql —— LTX 数据源（第二个 ed2k 来源）的本地镜像
--
-- 为什么要在本地存一份，而不是每次翻页都打 AVdb：
--
--   1. **日期导航与月历要「哪几天有货、各有几条」**。AVdb 只提供
--      按站点/版块分页的列表，没有「按日期聚合计数」的接口；
--      把 658 条拉下来在本地 GROUP BY 是一次请求量级的成本，
--      而每次翻页、每次点月历都去问 AVdb 是几十次。
--   2. **「已下载」状态得落在自己库里**（tg_sent_at）。AVdb 不知道
--      我们有没有把这条 ed2k 转发给 Telegram，这个状态没人替我们记。
--   3. AVdb 挂了 / 手机关掉电脑后仍要能翻已同步的内容。
--
-- 镜像与 4K 的 posts 表**分开**（不复用 posts）：
--   - 主键语义不同：AVdb 的资源 tid 是 16 位合成 ID，与论坛 tid 无关；
--   - 列不同：AVdb 给的是 publish_date/magnet/preview_images，没有
--     演员、発売日、状态机、重试等字段；
--   - 生命周期不同：这里整表按同步结果覆盖式更新（rejected 除外），
--     posts 表则是逐帖采集、逐帖删除。
--   混进一张表会让「删除 / 采集 / Emby 同步」这些流程都得先判断来源。

CREATE TABLE ltx_items (
  id            INTEGER PRIMARY KEY,        -- AVdb 资源 tid（合成 ID，非论坛 tid）
  number        TEXT,                       -- 番号，如 SSIS-157
  title         TEXT    NOT NULL DEFAULT '',
  section       TEXT,                       -- 版块名（LTX破解）
  publish_date  TEXT    NOT NULL,           -- AVdb publish_date，YYYY-MM-DD
  size_bytes    INTEGER,                    -- ed2k 里的大小（字节）
  ed2k          TEXT,                       -- 下载链接（LTX 全是 ed2k，字段名沿用 AVdb 的 magnet）
  cover_url     TEXT,                       -- 封面源 URL（AVdb preview_images）
  detail_url    TEXT,                       -- JavDB 详情页（留档，界面暂不展示）
  rejected      INTEGER NOT NULL DEFAULT 0, -- AVdb 侧的「不合格资源」标记，1 = 不展示
  created_at    TEXT,                       -- AVdb create_time（同日排序用）
  tg_sent_at    TEXT,                       -- NULL = 未转发；转发后写时间
  fetched_at    TEXT    NOT NULL            -- 本行最后一次同步时间
);

-- 浏览路径只查未排除的行（与 4K 的 idx_browse 同思路：部分索引）
CREATE INDEX idx_ltx_date ON ltx_items(publish_date) WHERE rejected = 0;
