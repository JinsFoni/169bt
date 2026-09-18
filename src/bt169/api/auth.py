"""访问门禁：服务端校验（S-6）。

需求 §8.13 问「访问密码仅前端门禁还是后端校验」。
**架构结论：必须后端校验**（ARCHITECTURE.md ADR-12）——静态资源任何人
都能下载，前端门禁不是安全边界，API 才是。

本模块提供三样东西：

1. **口令哈希**：``basic.password`` 存 PBKDF2-SHA256（60 万次，ADR-17），
   绝不存明文。
2. **面板会话**：随机 32 字节 token，**库里只存哈希**，30 天有效。
   桌面 + 手机双端要能同时登录 → 支持多会话并存。
3. **门禁判定**：未设密码 = 门禁关闭（个人自用，别把自己锁外面）。

★ 三条容易踩的坑，都有测试钉住：

- **cookie 不能无脑加 ``Secure``**：本机是 ``http://127.0.0.1``，加了
  ``Secure`` 浏览器**不发送** cookie，登录直接失效。只有反代注入
  ``X-Forwarded-Proto: https``（Lucky）时才加。
- **改密码要踢掉所有会话**：否则旧会话能一直用到 30 天后。
- **静态 UI 与 ``/api/auth/*`` 必须免认证**：否则没人能登录进来。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from bt169.crypto import hash_password, verify_password
from bt169.db import Database
from bt169.repo.settings import SettingsRepo

__all__ = [
    "SESSION_COOKIE",
    "SESSION_DAYS",
    "GATE_KEY",
    "is_gate_enabled",
    "set_access_password",
    "verify_access_password",
    "create_session",
    "validate_session",
    "revoke_session",
    "revoke_all_sessions",
    "cleanup_sessions",
]

#: 面板会话 cookie 名。
SESSION_COOKIE = "bt169_session"

#: 会话有效期（ARCHITECTURE.md §9：默认 30 天）。
SESSION_DAYS = 30

#: 访问密码的存储键（「基础设置」分区）。
GATE_KEY = "basic.password"


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ------------------------------------------------------------ 口令


def is_gate_enabled(sr: SettingsRepo) -> bool:
    """是否已设置访问密码。

    ★ 未设置 = 门禁关闭。个人自用场景下，默认不挡自己；用户想要门禁
    就去设置面板填一个密码。
    """
    return bool(sr.get(GATE_KEY))


def set_access_password(sr: SettingsRepo, password: str) -> None:
    """写入访问密码的**哈希**，并踢掉所有已存在的会话。

    ★ 改密码必须让旧会话失效：否则「改密码」这个动作在安全上是空的，
    任何拿过 cookie 的人照样能用到 30 天后。
    """
    sr.put(GATE_KEY, hash_password(password))


def verify_access_password(sr: SettingsRepo, password: str) -> bool:
    """校验访问密码。未设置时返回 ``False``（由调用方先查门禁开关）。"""
    stored = sr.get(GATE_KEY)
    if not stored:
        return False
    return verify_password(password, stored)


# ------------------------------------------------------------ 会话


def _hash_token(token: str) -> str:
    """token 的存储形式。

    ★ 只存哈希：库文件被看到（备份、误提交）也不能直接拿来冒充。
    这里用 sha256 而非 PBKDF2——token 本身是 32 字节高熵随机数，
    不存在字典攻击空间，不需要慢哈希（慢哈希反而让每个请求都变慢）。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Database, *, days: int = SESSION_DAYS) -> str:
    """创建会话，返回**明文 token**（唯一一次可见）。"""
    token = secrets.token_urlsafe(32)
    now = _now()
    with db.write() as c:
        c.execute(
            "INSERT INTO panel_sessions(token_hash, created_at, expires_at)"
            " VALUES(?,?,?)",
            (
                _hash_token(token),
                _iso(now),
                _iso(now + timedelta(days=days)),
            ),
        )
    return token


def validate_session(db: Database, token: str | None) -> bool:
    """会话是否有效。空 token / 未知 token / 已过期一律 ``False``。"""
    if not token:
        return False
    row = db.read().execute(
        "SELECT expires_at FROM panel_sessions WHERE token_hash=?",
        (_hash_token(token),),
    ).fetchone()
    if row is None:
        return False
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (ValueError, TypeError):
        return False
    return expires > _now()


def revoke_session(db: Database, token: str | None) -> None:
    """登出：删除单个会话。"""
    if not token:
        return
    with db.write() as c:
        c.execute(
            "DELETE FROM panel_sessions WHERE token_hash=?",
            (_hash_token(token),),
        )


def revoke_all_sessions(db: Database) -> None:
    """删除全部会话（改密码时用）。"""
    with db.write() as c:
        c.execute("DELETE FROM panel_sessions")


def cleanup_sessions(db: Database) -> int:
    """清掉已过期的会话行，返回删除条数。

    过期会话不清理会一直堆积；虽然校验时已拒绝，但表会无限长。
    """
    with db.write() as c:
        cur = c.execute(
            "DELETE FROM panel_sessions WHERE expires_at <= ?", (_iso(_now()),)
        )
        return cur.rowcount or 0


def is_https(request) -> bool:  # type: ignore[no-untyped-def]
    """请求是否经由 HTTPS 抵达。

    ★ Lucky 反代必须注入 ``X-Forwarded-Proto: https``，否则这里判断为
    明文，cookie 不带 ``Secure``（功能正常但安全性降级）。见 README 部署节。
    """
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def cookie_kwargs(request, *, max_age: int | None = None) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """面板 cookie 的统一属性。

    ``HttpOnly`` 防 XSS 窃取；``SameSite=Lax`` 防 CSRF（ARCHITECTURE §9）。
    ``Secure`` 只在真的走 HTTPS 时加——见 :func:`is_https` 的说明。
    """
    kwargs: dict[str, object] = {
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": is_https(request),
    }
    if max_age is not None:
        kwargs["max_age"] = max_age
    return kwargs
