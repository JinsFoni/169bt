-- 005_fix_size.sql —— 洗掉 size 里的 @注解（数据修复）
--
-- 站点把「值 + 补充说明」写成一行，用 @ 分隔：
--
--     [影片大小]：7GB@NO Watermark
--
-- 解析层曾经把整行（含注解）存进 posts.size，于是库里躺着
-- '7GB@NO Watermark'、'14GB@NO Watermark' 这种值，前端卡片
-- 「影片大小」后面就会多出一截不该出现的字符。
--
-- 解析层已在 source/parse.py 的 _strip_annotation() 里修好，
-- 但**已经进库的行不会自己变干净**，所以这里补一次数据修复。
--
-- instr(size,'@') 找不到 @ 时返回 0，substr(size,1,-1) 会得到空串。
-- 靠 WHERE size LIKE '%@%' 兜住，只碰真正带注解的行——没有 @ 的行
-- 和 NULL 行都原样不动。
--
-- 幂等：跑第二遍时 '7GB' 不含 @，被 WHERE 滤掉。

UPDATE posts
   SET size = NULLIF(trim(substr(size, 1, instr(size, '@') - 1)), '')
 WHERE size LIKE '%@%';
