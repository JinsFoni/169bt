-- 004_auth.sql —— 访问门禁（S-6）
--
-- 面板会话表。**只存 token 的 sha256 哈希**，不存明文 token：
-- 库文件被看到（备份、误提交、被别人摸到 data/）也不能直接拿来冒充。
--
-- 为什么不用 PBKDF2 存 token：
--   token 本身是 secrets.token_urlsafe(32) 的高熵随机串，不存在字典
--   攻击空间。慢哈希在这里没有收益，只会让**每个请求**都慢一截。
--   （口令是另一回事——低熵、可枚举，所以 basic.password 必须 PBKDF2。）
--
-- 多行 = 支持多设备并存：桌面 + 手机同时登录（FRONTEND.md 的双端要求）。
-- 改访问密码时整表清空，把所有设备踢下线。

CREATE TABLE panel_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- 过期清理（cleanup_sessions）与按 token 查（每次请求）都要走索引
CREATE INDEX idx_sessions_expires ON panel_sessions(expires_at);
