"""S-6 访问门禁：服务端校验。

★ 这一组是**安全边界**测试（ARCHITECTURE.md §9 / ADR-12）。

需求 §8.13 问「访问密码仅前端门禁还是后端校验」——架构结论是
**必须后端校验**：静态资源任何人都能下载，前端门禁不是边界。
所以这里的每一条都必须在**没有会话**的前提下断言「拿不到数据」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bt169.api.app import create_app
from bt169.crypto import SecretBox, verify_password

TEST_KEY = b"\x09" * 32


@pytest.fixture()
def box():
    return SecretBox(TEST_KEY)


@pytest.fixture()
def ui_dir(tmp_path):
    """一个最小的前端目录，用来验证「静态资源免认证」。"""
    d = tmp_path / "ui"
    d.mkdir()
    (d / "index.html").write_text("<html>登录页</html>", encoding="utf-8")
    (d / "app.js").write_text("// x", encoding="utf-8")
    return d


@pytest.fixture()
def img_dir(tmp_path):
    d = tmp_path / "images"
    d.mkdir()
    (d / "ab").mkdir()
    (d / "ab" / "abc-600.webp").write_bytes(b"RIFFxxxxWEBP")
    return d


def make_client(db, box, ui_dir=None, image_dir=None) -> TestClient:
    app = create_app(db, box=box, ui_dir=ui_dir, image_dir=image_dir)
    return TestClient(app)


# ------------------------------------------------------------ 口令哈希


def test_access_password_is_hashed_not_plaintext(db, box):
    """★ 落库的必须是 PBKDF2 哈希，不是明文（ADR-17）。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    set_access_password(sr, "hunter2")

    stored = sr.get("basic.password")
    assert stored is not None
    assert "hunter2" not in stored
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("hunter2", stored) is True


def test_verify_access_password_rejects_wrong(db, box):
    from bt169.api.auth import set_access_password, verify_access_password
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    set_access_password(sr, "hunter2")
    assert verify_access_password(sr, "hunter2") is True
    assert verify_access_password(sr, "Hunter2") is False
    assert verify_access_password(sr, "") is False


def test_gate_disabled_when_no_password(db, box):
    """没设访问密码 → 门禁关闭（个人自用，默认不挡自己）。"""
    from bt169.api.auth import is_gate_enabled
    from bt169.repo.settings import SettingsRepo

    assert is_gate_enabled(SettingsRepo(db, box)) is False


def test_gate_enabled_after_setting(db, box):
    from bt169.api.auth import is_gate_enabled, set_access_password
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    set_access_password(sr, "hunter2")
    assert is_gate_enabled(sr) is True


# ------------------------------------------------------------ 会话存储


def test_session_token_stored_hashed(db, box):
    """★ 会话 token 只存哈希：库被看到也不能直接拿来冒充（§9）。"""
    from bt169.api.auth import create_session

    token = create_session(db)
    rows = db.read().execute("SELECT token_hash FROM panel_sessions").fetchall()
    assert len(rows) == 1
    assert rows[0]["token_hash"] != token
    assert token not in rows[0]["token_hash"]


def test_validate_session_roundtrip(db, box):
    from bt169.api.auth import create_session, validate_session

    token = create_session(db)
    assert validate_session(db, token) is True
    assert validate_session(db, "nope") is False
    assert validate_session(db, "") is False


def test_expired_session_is_rejected(db, box):
    """过期会话必须失效，否则 30 天等于永久。"""
    from datetime import datetime, timedelta

    from bt169.api.auth import create_session, validate_session

    token = create_session(db)
    past = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(
        timespec="seconds"
    )
    with db.write() as c:
        c.execute("UPDATE panel_sessions SET expires_at=?", (past,))
    assert validate_session(db, token) is False


def test_revoke_session(db, box):
    from bt169.api.auth import create_session, revoke_session, validate_session

    token = create_session(db)
    revoke_session(db, token)
    assert validate_session(db, token) is False


def test_revoke_all_sessions(db, box):
    """改访问密码必须能把所有设备踢下线。"""
    from bt169.api.auth import (
        create_session,
        revoke_all_sessions,
        validate_session,
    )

    a, b = create_session(db), create_session(db)
    revoke_all_sessions(db)
    assert validate_session(db, a) is False
    assert validate_session(db, b) is False


def test_multiple_sessions_coexist(db, box):
    """桌面 + 手机双端（FRONTEND.md）→ 必须支持并发会话。"""
    from bt169.api.auth import create_session, validate_session

    a, b = create_session(db), create_session(db)
    assert validate_session(db, a) is True
    assert validate_session(db, b) is True


# ------------------------------------------------------------ 门禁开关


def test_open_when_gate_disabled(db, box):
    """未设密码时 /api/posts 照常可读（不能把自己锁在门外）。"""
    c = make_client(db, box)
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200


def test_blocks_api_when_gate_enabled(db, box):
    """★ 核心断言：设了密码又没有会话 → 数据拿不到。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    r = c.get("/api/posts", params={"date": "2026-09-14"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_blocks_settings_when_gate_enabled(db, box):
    """设置里有论坛账号密码——更不能裸奔。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    assert c.get("/api/settings").status_code == 401


def test_blocks_images_when_gate_enabled(db, box, img_dir):
    """★ /img 是用户内容（不是「静态资源」），同样必须挡。

    把本地图片当普通静态资源免认证，等于把整个图库公开。
    """
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box, image_dir=img_dir)
    assert c.get("/img/ab/abc-600.webp").status_code == 401


def test_images_served_with_session(db, box, img_dir):
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box, image_dir=img_dir)
    c.post("/api/auth/login", json={"password": "hunter2"})
    r = c.get("/img/ab/abc-600.webp")
    assert r.status_code == 200


def test_auth_routes_always_reachable(db, box):
    """★ 门禁开着时 /api/auth/* 必须可达，否则没人能登录进来。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    assert c.get("/api/auth/me").status_code == 200


def test_static_ui_reachable_without_session(db, box, ui_dir):
    """★ 登录页本身是静态资源——挡掉它就没法登录了。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box, ui_dir=ui_dir)
    assert c.get("/index.html").status_code == 200
    assert c.get("/app.js").status_code == 200


# ------------------------------------------------------------ 登录 / 登出


def test_login_with_correct_password_sets_cookie(db, box):
    from bt169.api.auth import SESSION_COOKIE, set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    r = c.post("/api/auth/login", json={"password": "hunter2"})
    assert r.status_code == 200
    assert SESSION_COOKIE in r.cookies or SESSION_COOKIE in c.cookies
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200


def test_login_with_wrong_password_401(db, box):
    from bt169.api.auth import SESSION_COOKIE, set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    r = c.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert SESSION_COOKIE not in c.cookies
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 401


def test_cookie_is_httponly_and_samesite_lax(db, box):
    """★ HttpOnly（防 XSS 窃取）+ SameSite=Lax（防 CSRF，§9）。"""
    from bt169.api.auth import SESSION_COOKIE, set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    raw = c.post("/api/auth/login", json={"password": "hunter2"}).headers[
        "set-cookie"
    ].lower()
    assert SESSION_COOKIE.lower() in raw
    assert "httponly" in raw
    assert "samesite=lax" in raw


def test_cookie_not_secure_over_plain_http(db, box):
    """★ 本机是 http://127.0.0.1——此时加 Secure 会让浏览器**不发送**
    cookie，登录直接失效。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    raw = c.post("/api/auth/login", json={"password": "hunter2"}).headers[
        "set-cookie"
    ].lower()
    assert "secure" not in raw


def test_cookie_secure_behind_https_proxy(db, box):
    """★ Lucky 反代注入 ``X-Forwarded-Proto: https`` 时必须加 Secure。"""
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    raw = c.post(
        "/api/auth/login",
        json={"password": "hunter2"},
        headers={"X-Forwarded-Proto": "https"},
    ).headers["set-cookie"].lower()
    assert "secure" in raw


def test_logout_revokes_session(db, box):
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    c.post("/api/auth/login", json={"password": "hunter2"})
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 401


def test_me_reports_state(db, box):
    from bt169.api.auth import set_access_password
    from bt169.repo.settings import SettingsRepo

    c = make_client(db, box)
    assert c.get("/api/auth/me").json() == {
        "authenticated": True,          # 门禁关闭 → 视为已认证
        "gate_enabled": False,
    }

    set_access_password(SettingsRepo(db, box), "hunter2")
    assert c.get("/api/auth/me").json() == {
        "authenticated": False,
        "gate_enabled": True,
    }
    c.post("/api/auth/login", json={"password": "hunter2"})
    assert c.get("/api/auth/me").json() == {
        "authenticated": True,
        "gate_enabled": True,
    }


# ------------------------------------------------------------ 设置面板写入


def test_settings_save_hashes_access_password(db, box):
    """★ 从设置面板保存访问密码也必须哈希。

    前端 FIELDS 里 basic 分区就一个 ``password`` 字段——如果这里原样
    落库，门禁就退化成明文比对。
    """
    from bt169.repo.settings import SettingsRepo

    c = make_client(db, box)
    r = c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "s3cret"}},
    )
    assert r.status_code == 200

    stored = SettingsRepo(db, box).get("basic.password")
    assert stored is not None
    assert "s3cret" not in stored
    assert verify_password("s3cret", stored) is True


def test_saving_new_password_invalidates_sessions(db, box):
    """★ 改密码就该把所有设备踢下线——否则旧会话能一直用下去。"""
    from bt169.repo.settings import SettingsRepo

    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "old"}},
    )
    c.post("/api/auth/login", json={"password": "old"})
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200

    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "new"}},
    )
    assert c.get(
        "/api/posts", params={"date": "2026-09-14"}
    ).status_code == 401, "改密码后旧会话必须失效"


def test_clearing_access_password_opens_gate(db, box):
    """清空访问密码 → 门禁关闭（可自救，不会永久锁死）。

    ★ 注意必须先登录再清空：门禁一旦开启，**未认证的 PUT 也会被拦**
    （这是对的——否则攻击者能直接关掉门禁）。所以「自救」的正确姿势是
    「用刚设的密码登录 → 再清空」，而不是无凭证硬改。
    """
    from bt169.repo.settings import SettingsRepo

    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "x"}},
    )
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 401

    # 用刚设的密码登录，拿回会话
    assert c.post("/api/auth/login", json={"password": "x"}).status_code == 200

    c.put("/api/settings", json={"section": "basic", "values": {"password": ""}})
    # 清空后门禁关闭，无需会话也能读
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200
    assert c.get("/api/auth/me").json() == {
        "authenticated": True,
        "gate_enabled": False,
    }


def test_unauthenticated_cannot_disable_gate(db, box):
    """★ 未认证不能关掉门禁——否则门禁形同虚设。"""
    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "x"}},
    )
    r = c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": ""}},
    )
    assert r.status_code == 401

    from bt169.repo.settings import SettingsRepo

    assert SettingsRepo(db, box).get("basic.password") is not None, (
        "门禁必须仍然生效"
    )


def test_login_when_gate_disabled_succeeds(db, box):
    """门禁关闭时登录不该报错（前端会无脑调一次）。"""
    c = make_client(db, box)
    assert c.post("/api/auth/login", json={"password": "whatever"}).status_code == 200


# ------------------------------------------------------------ CSRF


def test_cross_origin_write_is_rejected(db, box):
    """★ SameSite=Lax 之外再加一道：写操作的 Origin 必须同源（§9）。"""
    c = make_client(db, box)
    r = c.post(
        "/api/collect",
        json={"from_date": "2026-09-14", "to_date": "2026-09-14"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "bad_origin"


def test_same_origin_write_is_allowed(db, box):
    c = make_client(db, box)
    r = c.post(
        "/api/collect",
        json={"from_date": "2026-09-14", "to_date": "2026-09-14"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code != 403


def test_read_is_not_origin_checked(db, box):
    """GET 是安全方法，跨源读由 SameSite 管，不该被 Origin 拦。"""
    c = make_client(db, box)
    r = c.get(
        "/api/posts",
        params={"date": "2026-09-14"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 200


# ------------------------------------------------------------ 占位符不得覆盖
#
# ★ 这一组防的是**我自己刚引入的锁死事故**：设置面板每次保存都会把
#   密钥字段回传成占位符（••••••••）。如果哈希发生在「跳过占位符」之前，
#   用户只是点了一下保存、什么都没改，访问密码就被换成占位符的哈希——
#   再也进不来了，而且所有会话都被踢下线。
#
#   原有的 test_settings_save_hashes_access_password 用的是真密码，
#   永远走不到占位符分支 → 测试全绿而线上锁死。


def test_saving_placeholder_keeps_password(db, box):
    """★ 回传占位符 = 未修改，密码必须原样保留。"""
    from bt169.config import PLACEHOLDER
    from bt169.repo.settings import SettingsRepo

    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "my-real-pass"}},
    )
    before = SettingsRepo(db, box).get("basic.password")

    c.post("/api/auth/login", json={"password": "my-real-pass"})
    r = c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": PLACEHOLDER}},
    )
    assert r.status_code == 200
    assert r.json()["skipped"] == ["password"], "占位符应被识别为未修改"

    after = SettingsRepo(db, box).get("basic.password")
    assert after == before, "占位符不该改动密码"
    assert verify_password("my-real-pass", after) is True
    assert verify_password(PLACEHOLDER, after) is False, "密码绝不能被改成占位符"


def test_saving_placeholder_does_not_revoke_sessions(db, box):
    """★ 什么都没改 → 不该把人踢下线。"""
    from bt169.config import PLACEHOLDER

    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "my-real-pass"}},
    )
    c.post("/api/auth/login", json={"password": "my-real-pass"})
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200

    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": PLACEHOLDER}},
    )
    assert c.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200, (
        "未修改密码不该踢掉会话"
    )


def test_placeholder_after_login_can_still_login(db, box):
    """★ 端到端复现：设密码 → 登录 → 保存占位符 → 还能用原密码登录。"""
    from bt169.config import PLACEHOLDER

    c = make_client(db, box)
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": "letmein"}},
    )
    c.post("/api/auth/login", json={"password": "letmein"})
    c.put(
        "/api/settings",
        json={"section": "basic", "values": {"password": PLACEHOLDER}},
    )

    fresh = make_client(db, box)
    r = fresh.post("/api/auth/login", json={"password": "letmein"})
    assert r.status_code == 200, "原密码必须仍然有效——否则用户被永久锁死"
    assert fresh.get("/api/posts", params={"date": "2026-09-14"}).status_code == 200


def test_logout_clears_cookie_header(db, box):
    """★ 登出必须**真的清掉浏览器里的 cookie**。

    只撤销服务端会话是不够的：cookie 还在，浏览器会继续带着它发请求，
    用户看到的是「登出后又刷新一下，还是登录态」——因为 `/api/auth/me`
    虽然会返回 false，但下次登录前那串无效 cookie 一直挂着。
    正确做法是回一个 `Set-Cookie` 把它清空。

    ★ 这个 bug 的成因值得记下来：处理函数里改的是**注入的 `response`
    对象**，但最后 `return Response(204)` 新建了一个——注入对象的
    改动被丢掉了。测试原来只查「服务端会话是否失效」，所以全绿。
    """
    from bt169.api.auth import SESSION_COOKIE, set_access_password
    from bt169.repo.settings import SettingsRepo

    set_access_password(SettingsRepo(db, box), "hunter2")
    c = make_client(db, box)
    c.post("/api/auth/login", json={"password": "hunter2"})

    r = c.post("/api/auth/logout")
    assert r.status_code == 204
    raw = r.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE.lower() in raw, "登出必须下发 Set-Cookie 清空会话"
    # 清空的两种合法写法：max-age=0 或 expires 已过期
    assert "max-age=0" in raw or "expires=" in raw, raw
    assert SESSION_COOKIE not in c.cookies, "客户端 cookie 应被删除"
