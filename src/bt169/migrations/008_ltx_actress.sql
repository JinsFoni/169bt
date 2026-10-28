-- 008_ltx_actress.sql —— LTX 资源也存演员（与 4K 卡片同一份展示能力）
--
-- 为什么落到表里，而不是像第一版那样「打开灯箱时现查」：
--
--   1. **卡片上就要显示**。4K 的演员是采集时抓下来的，卡片头一行就能
--      看到「SSIS-157 三上悠亜」；LTX 若只有灯箱里有，两页长得不一样。
--   2. **同一条会被反复查看**。现查一次 0.6–14 秒（javdb 侧未缓存），
--      而这条资源可能被翻来覆去点开十几次。落库之后重开是 0 成本。
--   3. javdb 侧的额度是用户自己的账号，能少问就少问。
--
-- 两个列的分工（★ 别把 NULL 当「查不到」）：
--
--   actress     NULL + actress_at IS NULL  → **还没查过**（待回填）
--   actress     NULL + actress_at 有值     → **查过了，上游没有/对不上**
--   actress     '名字、名字'               → 查到了
--
-- 少了 actress_at 就分不开前两种，回填程序会把「确认没有」的行反复重查，
-- 每天把上游白问一遍。
--
-- 回填是**串行**的（实测 6 路并发打 javdb 详情 → 40/40 全 502，串行 +0.5
-- 秒则全成功），所以这一列同时是「进度游标」：未填的行按 publish_date
-- 倒序处理，用户正在看的那几天最先有。

ALTER TABLE ltx_items ADD COLUMN actress TEXT;
ALTER TABLE ltx_items ADD COLUMN actress_at TEXT;

-- 回填的取数路径：`WHERE actress_at IS NULL ORDER BY publish_date DESC`。
-- 部分索引只覆盖待回填的行（做完的行会从这个索引里消失，越来越小）。
CREATE INDEX idx_ltx_actress_pending
  ON ltx_items(publish_date DESC) WHERE actress_at IS NULL;
