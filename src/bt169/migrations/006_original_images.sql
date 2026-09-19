-- 006_original_images.sql —— 灯箱显示原图
--
-- 需求：点击卡片后的大图要显示**原图**，不能是缩放过的糊图。
--
-- 为什么加两列而不是复用 cover_local / detail_local：
--   卡片缩略图（600px，67 KB）与灯箱原图（~475 KB）用途完全不同。
--   若只留一列，卡片就得加载原图 —— 单页体积从 1 MB 涨到 11 MB，
--   而卡片只用到 1.8% 的像素（实测：卡片显示 300×200，源图 2184×1542）。
--   两列并存，各取所需。
--
-- 为什么灯箱的封面图要单独一列：
--   灯箱同时显示封面图与详情图，两张都按 ~1129 CSS px 渲染。
--   而 cover_local 是给卡片用的 600px 档 → 放大 1.88×（DPR=2 时 3.8×）。
--   实测就是这个原因导致封面图糊。
--
-- 详情图的源图本身就是 867×1078（图床上限），原图档等于原尺寸，
-- 但加上它仍有意义：灯箱不再把 867px 拉伸到 1129px 显示。

ALTER TABLE posts ADD COLUMN cover_orig  TEXT;
ALTER TABLE posts ADD COLUMN detail_orig TEXT;

-- 供 169bt doctor 统计原图档覆盖率
CREATE INDEX idx_orig_cover ON posts(cover_orig) WHERE cover_orig IS NULL;
