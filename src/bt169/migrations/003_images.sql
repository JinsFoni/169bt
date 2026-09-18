-- 003_images.sql —— 图片本地化（W-15）
--
-- 存**两套**路径，而不是替换掉源 URL：
--   cover_img / detail_img  : 图床源 URL（**引用计数的依据**）
--   cover_local / detail_local : 本地 WebP 相对路径（/img/ab/ab3f-600.webp）
--
-- 为什么不直接覆盖源 URL：
--   1. 硬删除时要做引用计数（同一张图可能被多个帖子引用）。
--      计数必须按**稳定的标识**做，本地路径是从源 URL 派生的，
--      按源 URL 计数才是精确的。
--   2. 图床挂了但本地已有图时，重采能靠源 URL 判断「这张图我下过了」。
--   3. 出问题时能追溯原图。

ALTER TABLE posts ADD COLUMN cover_local  TEXT;
ALTER TABLE posts ADD COLUMN detail_local TEXT;

-- 供 169bt doctor 统计本地化覆盖率
CREATE INDEX idx_local_cover ON posts(cover_local) WHERE cover_local IS NULL;
