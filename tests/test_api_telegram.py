"""Telegram 转发接口（T-1~T-8）。

全部离线：把 ``build_client`` 换成假客户端，不打真实 Bot API。
"""

from __future__ import annotations

import json

import pytest

from bt169 import config
from bt169.api.routes import telegram as tg_routes
from bt169.repo.settings import SettingsRepo


class FakeHTTP:
    def __init__(self, *, status=200, body=None, raise_exc=None):
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, *, json=None, data=None, timeout=None):
        self.calls.append((url, json or data or {}))
        if self.raise_exc is not None:
            raise self.raise_exc

        class R:
            pass

        r = R()
        r.status_code = self.status
        r.text = self.body or '{"ok":true,"result":{"message_id":7}}'
        return r

    def close(self):
        pass


def seed_post(db, tid=101, *, ed2k="ed2k://|file|a.mkv|1|AB|/", date="2026-09-14"):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,code,actress,release_date,size,"
            " cover_img,detail_img,ed2k,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"标题 {tid}", f"ABC-{tid}", "某", "2026-09-17", "7GB",
             None, None, ed2k, date, "done", now_iso(), now_iso()),
        )


@pytest.fixture()
def tg_app(db, box, monkeypatch):
    """应用 + 假 MTProto 发送器（转发主链路，方案 A）。"""
    from fastapi.testclient import TestClient

    from bt169 import config as cfg
    from bt169.api.app import create_app
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender

    fake = FakeMtp()
    monkeypatch.setattr(
        tg_routes, "get_sender",
        lambda settings: MtpSender(client=fake, target="@nan_share_bot"),
    )

    sr = SettingsRepo(db, box)
    sr.put(cfg.section_key("tg", "api_id"), "12345")
    sr.put(cfg.section_key("tg", "api_hash"), "deadbeef")
    sr.put(cfg.section_key("tg", "session"), "s")
    sr.put(cfg.section_key("tg", "target"), "@nan_share_bot")

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        c.fake = fake        # type: ignore[attr-defined]
        c.db = db            # type: ignore[attr-defined]
        c.box = box          # type: ignore[attr-defined]
        yield c


def set_tg(db, box):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "api_id"), "12345")
    sr.put(config.section_key("tg", "api_hash"), "deadbeef")
    sr.put(config.section_key("tg", "session"), "s")
    sr.put(config.section_key("tg", "target"), "@nan_share_bot")


# ---------------------------------------------------------------- 单帖转发


def test_forward_one_ok(tg_app):
    seed_post(tg_app.db, 101)
    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "message_id": 42}
    assert len(tg_app.fake.calls) == 1


def test_forward_one_marks_tg_sent(tg_app):
    seed_post(tg_app.db, 101)
    tg_app.post("/api/posts/101/forward")

    from bt169.repo.posts import PostRepo
    assert PostRepo(tg_app.db).get(101).tg_sent_at is not None


def test_forward_one_404_for_unknown_tid(tg_app):
    r = tg_app.post("/api/posts/999/forward")
    assert r.status_code == 404


def test_forward_one_twice_returns_409(tg_app):
    """★ 幂等：重复点「下载」→ 409，不重复发送（T-7）。"""
    seed_post(tg_app.db, 101)
    tg_app.post("/api/posts/101/forward")
    r = tg_app.post("/api/posts/101/forward")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "tg_already_sent"
    assert len(tg_app.fake.calls) == 1, "第二次不该真的发请求"


def test_forward_one_without_ed2k_returns_502(tg_app):
    """★ 未解锁帖不可转发（T-5）。"""
    seed_post(tg_app.db, 101, ed2k=None)
    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 502
    assert "ed2k" in r.json()["error"]["message"]


def test_forward_one_not_configured_returns_400(tg_app, monkeypatch):
    """★ 未配置 TG → 400 + tg_not_configured，前端据此引导去设置（T-4）。"""
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpNotConfigured

    def boom(settings):
        raise MtpNotConfigured("尚未配置 MTProto")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


def test_forward_one_rate_limited_returns_429_with_retry_after(tg_app, monkeypatch):
    from telethon.errors import FloodWaitError

    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender

    def boom(settings):
        return MtpSender(
            client=FakeMtp(exc=FloodWaitError(request=None, capture=42)),
            target="@x",
        )

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 429
    detail = r.json()["error"]
    assert detail["code"] == "tg_rate_limited"
    assert detail["retry_after"] == 42


def test_forward_one_bot_error_returns_502(tg_app, monkeypatch):
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender

    def boom(settings):
        return MtpSender(client=FakeMtp(exc=ValueError("boom")), target="@x")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "tg_error"


# ---------------------------------------------------------------- 批量转发


def test_forward_day_ok(tg_app):
    for tid in (101, 102):
        seed_post(tg_app.db, tid)
    r = tg_app.post("/api/archive/forward", params={"date": "2026-09-14"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] == 2
    assert body["failed"] == 0


def test_forward_day_partial_failure_still_200(tg_app):
    """★ 部分失败返回 200 而非 5xx——批量里「有些成功」是正常结果（T-6）。"""
    seed_post(tg_app.db, 101)
    seed_post(tg_app.db, 102, ed2k=None)   # 必然失败
    seed_post(tg_app.db, 103)

    r = tg_app.post("/api/archive/forward", params={"date": "2026-09-14"})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 2
    assert body["failed"] == 1
    assert body["errors"][0]["tid"] == 102


def test_forward_day_empty_returns_404(tg_app):
    r = tg_app.post("/api/archive/forward", params={"date": "2020-01-01"})
    assert r.status_code == 404


def test_forward_day_not_configured_returns_400(tg_app, monkeypatch):
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpNotConfigured

    def boom(settings):
        raise MtpNotConfigured("未配置")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/archive/forward", params={"date": "2026-09-14"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


def test_forward_day_skips_already_sent(tg_app):
    for tid in (101, 102):
        seed_post(tg_app.db, tid)
    tg_app.post("/api/posts/101/forward")

    r = tg_app.post("/api/archive/forward", params={"date": "2026-09-14"})
    body = r.json()
    assert body["skipped"] == 1
    assert body["sent"] == 1


# ---------------------------------------------------------------- 测试接口


def test_settings_test_telegram_ok(tg_app):
    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_settings_test_telegram_mtp_failure_returns_ok_false(tg_app, monkeypatch):
    """★ 测试接口失败返回 200 + ok:false，不是 5xx。

    设置面板的「测试」按钮要**显示**失败原因，用错误状态码会让前端
    走通用错误分支、丢掉 detail。新契约：MTP / Bot 两条链路独立报告。
    """
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender
    from telethon.errors import FloodWaitError

    def boom(settings):
        return MtpSender(
            client=FakeMtp(exc=FloodWaitError(request=None, capture=9)),
            target="@x",
        )

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["channels"]["mtp"]["ok"] is False
    assert "限流" in body["channels"]["mtp"]["detail"]


def test_settings_test_telegram_not_configured_ok_false(tg_app, monkeypatch):
    """MTP 与 Bot 都未配置 → 两条链路都失败，ok:false。"""
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpNotConfigured

    def boom(settings):
        raise MtpNotConfigured("尚未配置")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["channels"]["mtp"]["ok"] is False
    assert body["channels"]["bot"]["ok"] is False


# ---------------------------------------------------------------- MTProto 转发（方案 A）


class FakeMtp:
    """替身 MtpSender 工厂所需的最小客户端。"""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send_message(self, entity, text):
        self.calls.append((entity, text))
        if self.exc is not None:
            raise self.exc

        class M:
            id = 42
        return M()


@pytest.fixture()
def mtp_app(db, box, monkeypatch):
    """应用 + 假 MtpSender：转发走 MTProto 传输层。"""
    from fastapi.testclient import TestClient

    from bt169 import config as cfg
    from bt169.api.app import create_app
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender
    from bt169.repo.settings import SettingsRepo

    fake = FakeMtp()
    monkeypatch.setattr(
        tg_routes, "get_sender",
        lambda settings: MtpSender(client=fake, target="@nan_share_bot"),
    )

    sr = SettingsRepo(db, box)
    sr.put(cfg.section_key("tg", "api_id"), "12345")
    sr.put(cfg.section_key("tg", "api_hash"), "deadbeef")
    sr.put(cfg.section_key("tg", "session"), "s")
    sr.put(cfg.section_key("tg", "target"), "@nan_share_bot")

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        c.fake = fake          # type: ignore[attr-defined]
        c.db = db              # type: ignore[attr-defined]
        c.box = box            # type: ignore[attr-defined]
        yield c


def test_forward_one_via_mtp_sends_plain_link(mtp_app):
    """方案 A：转发经 MTProto，消息体 = 纯 ed2k，目标 = tg.target。"""
    seed_post(mtp_app.db, 101)
    r = mtp_app.post("/api/posts/101/forward")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "message_id": 42}
    assert mtp_app.fake.calls == [
        ("@nan_share_bot", "ed2k://|file|a.mkv|1|AB|/")
    ]


def test_mtp_not_configured_returns_400(mtp_app, monkeypatch):
    """未配置 MTProto → 400 tg_not_configured（复用错误码）。"""
    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpNotConfigured

    def boom(settings):
        raise MtpNotConfigured("尚未配置 MTProto")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(mtp_app.db, 102)
    r = mtp_app.post("/api/posts/102/forward")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


def test_mtp_flood_wait_returns_429(mtp_app, monkeypatch):
    """FloodWait → 429 + retry_after（T-6 语义保持）。"""
    from telethon.errors import FloodWaitError

    from bt169.api.routes import telegram as tg_routes
    from bt169.mtpproto import MtpSender

    def boom(settings):
        return MtpSender(client=FakeMtp(exc=FloodWaitError(request=None, capture=77)),
                         target="@nan_share_bot")

    monkeypatch.setattr(tg_routes, "get_sender", boom)
    seed_post(mtp_app.db, 103)
    r = mtp_app.post("/api/posts/103/forward")
    assert r.status_code == 429
    detail = r.json()["error"]
    assert detail["code"] == "tg_rate_limited"
    assert detail["retry_after"] == 77


def test_settings_test_telegram_dual_channel_shape(tg_app):
    """新契约：channels.mtp / channels.bot 独立报告；detail 取 MTP 侧。"""
    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["channels"]["mtp"] == {"ok": True, "detail": "MTProto 消息已发送"}
    assert body["channels"]["bot"]["ok"] is False   # Bot 未配置只提示,不报错
    assert "未配置" in body["channels"]["bot"]["detail"]


# ---------------------------------------------------------------- 设置页内 MTProto 登录


@pytest.fixture()
def login_app(mtp_app, monkeypatch):
    """在 mtp_app 基础上注入假登录状态机。"""
    from bt169 import tglogin

    class FakeSigner:
        def __init__(self, **kw):
            FakeSigner.last = self
            self.need_password = False
            self.code_ok = True
            self.signed_in = False
            self.cancelled = False

        def send_code(self, phone):
            pass

        def sign_in(self, code):
            if not self.code_ok:
                from bt169.tglogin import MtpLoginError
                raise MtpLoginError("tg_code_invalid", "验证码错误")
            if self.need_password:
                from telethon.errors import SessionPasswordNeededError
                raise SessionPasswordNeededError(request=None)
            self.signed_in = True

        def sign_in_password(self, password):
            if password != "right":
                from bt169.tglogin import MtpLoginError
                raise MtpLoginError("tg_password_invalid", "两步验证密码错误")
            self.signed_in = True

        def session(self):
            return "SESSION-OK" if self.signed_in else ""

        def cancel(self):
            self.cancelled = True

    FakeSigner.last = None
    mgr = tglogin.MtpLoginManager(FakeSigner, start_cooldown=0)
    monkeypatch.setattr(tg_routes, "get_login_manager", lambda: mgr)
    mtp_app.mgr = mgr               # type: ignore[attr-defined]
    mtp_app.FakeSigner = FakeSigner  # type: ignore[attr-defined]
    return mtp_app


def test_tg_login_start_requires_api_credentials(login_app, box):
    """api_id/api_hash 未保存 → 400 + tg_not_configured。"""
    from bt169.repo.settings import SettingsRepo

    SettingsRepo(login_app.db, box).delete(config.section_key("tg", "api_id"))
    r = login_app.post(
        "/api/tg/login/start", json={"phone": "+8613800000000"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


def test_tg_login_full_flow_saves_session(login_app):
    """start → verify → 200；session 加密落库。"""
    r = login_app.post("/api/tg/login/start", json={"phone": "+8613800000000"})
    assert r.status_code == 200

    r = login_app.post("/api/tg/login/verify", json={"code": "12345"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # session 落库（加密）
    from bt169.repo.settings import SettingsRepo
    sr = SettingsRepo(login_app.db, login_app.box)
    assert sr.get(config.section_key("tg", "session")) == "SESSION-OK"
    # 落库后清掉临时状态
    assert login_app.mgr.active is False


def test_tg_login_need_password_branch(login_app):
    r = login_app.post("/api/tg/login/start", json={"phone": "+86138"})
    login_app.FakeSigner.last.need_password = True
    r = login_app.post("/api/tg/login/verify", json={"code": "1"})
    assert r.status_code == 200
    assert r.json() == {"need_password": True}

    r = login_app.post("/api/tg/login/password", json={"password": "right"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_tg_login_wrong_code_422_retryable(login_app):
    """验证码错 → 422 + tg_code_invalid；状态保留可重试。"""
    login_app.post("/api/tg/login/start", json={"phone": "+86138"})
    login_app.FakeSigner.last.code_ok = False
    r = login_app.post("/api/tg/login/verify", json={"code": "bad"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tg_code_invalid"
    # 可重试
    login_app.FakeSigner.last.code_ok = True
    r = login_app.post("/api/tg/login/verify", json={"code": "1"})
    assert r.status_code == 200


def test_tg_login_cooldown_maps_to_429(login_app):
    """★ 保护机制：冷却期内再点 → 429 + retry_after（前端禁用按钮）。"""
    from bt169 import tglogin
    from bt169.telegram import RateLimited

    mgr = tglogin.MtpLoginManager(login_app.FakeSigner, start_cooldown=30)
    login_app.app.state  # noqa: B018 — 占位避免误删
    import bt169.api.routes.telegram as tg_r
    tg_r.get_login_manager = lambda: mgr  # 不用 monkeypatch，直接覆盖引用

    r = login_app.post("/api/tg/login/start", json={"phone": "+86138"})
    assert r.status_code == 200
    r = login_app.post("/api/tg/login/start", json={"phone": "+86138"})
    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "tg_rate_limited"
    assert 0 < body["retry_after"] <= 31


def test_tg_login_cancel(login_app):
    login_app.post("/api/tg/login/start", json={"phone": "+86138"})
    r = login_app.post("/api/tg/login/cancel")
    assert r.status_code == 200
    assert login_app.mgr.active is False
