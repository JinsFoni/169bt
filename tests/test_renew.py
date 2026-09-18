"""会话主动续期（W-16）。

★ 登录是**稀缺资源**（实测 5 次 / 900 秒，按 IP）。续期逻辑必须极其保守：
宁可晚一天续，也不要烧掉额度。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bt169.source.session import (
    RELOGIN_BLOCKED,
    RELOGIN_FAILED,
    RELOGIN_OK,
    ForumSession,
    SessionRenewer,
    can_attempt_login,
)


def mk_session(*, days_left=30, valid=True, attempts_left=5,
               last_attempt=None, state=None, cookies=None):
    exp = datetime.now().astimezone() + timedelta(days=days_left)
    return ForumSession(
        cookies=cookies if cookies is not None else {"SlDj_2132_auth": "x"},
        username="ymxh",
        obtained_at=datetime.now().astimezone().isoformat(),
        expires_at=exp.isoformat(),
        valid=valid,
        login_attempts_left=attempts_left,
        last_login_attempt=last_attempt,
        last_relogin_at=None,
        relogin_state=state,
    )


class FakeStore:
    def __init__(self, session):
        self.session = session
        self.saved = []
        self.states = []

    def load(self):
        return self.session

    def set_relogin_state(self, state):
        self.states.append(state)
        self.session = None  # 简化：状态变更后不再返回会话


class FakeLogin:
    """假登录客户端。"""

    def __init__(self, ok=True, exc=None):
        self.ok = ok
        self.exc = exc
        self.calls = 0

    def login(self, username, password):
        self.calls += 1
        if self.exc:
            raise self.exc

        class R:
            pass

        r = R()
        r.ok = self.ok
        r.message = "欢迎您回来" if self.ok else "登录失败"
        r.username = "ymxh" if self.ok else None
        return r


def creds():
    return ("user", "pass")


# ---------------------------------------------------------------- 何时续期


def test_no_session_does_nothing():
    store = FakeStore(None)
    r = SessionRenewer(store=store, login_client=FakeLogin(),
                       credentials=creds).maybe_renew()
    assert r.attempted is False
    assert r.reason == "no_session"


def test_fresh_session_does_nothing():
    """还剩 30 天时**不该**续期——每次续期都烧一次登录额度。"""
    store = FakeStore(mk_session(days_left=30))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is False
    assert login.calls == 0
    assert r.reason == "not_needed"


def test_within_window_renews():
    """进入续期窗口（≤5 天）才动手。"""
    store = FakeStore(mk_session(days_left=4))
    login = FakeLogin(ok=True)
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is True
    assert login.calls == 1
    assert r.ok is True


def test_expired_session_renews():
    store = FakeStore(mk_session(days_left=-1))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is True


def test_invalid_session_renews_immediately():
    """会话已失效 → 立刻续期，与剩余天数无关。"""
    store = FakeStore(mk_session(days_left=30, valid=False))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is True


def test_no_expiry_treated_as_ok():
    """没有 expires_at（拿不到）时不该无脑续期。"""
    s = mk_session(days_left=30)
    object.__setattr__(s, "expires_at", None)
    store = FakeStore(s)
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is False
    assert login.calls == 0


# ---------------------------------------------------------------- 额度保护


def test_blocked_when_quota_low():
    """★ 额度 ≤1 时**绝不**尝试。额度是稀缺资源。"""
    store = FakeStore(mk_session(days_left=2, attempts_left=1))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is False
    assert r.reason == "quota"
    assert login.calls == 0, "额度不足时绝不能真的去登录"


def test_blocked_when_too_soon():
    """★ 距上次提交 < 600 秒时拒绝（服务端 900 秒窗口内重复提交是浪费）。"""
    recent = (datetime.now().astimezone()
              - timedelta(seconds=60)).isoformat()
    store = FakeStore(mk_session(days_left=2, last_attempt=recent))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=creds).maybe_renew()
    assert r.attempted is False
    assert r.reason == "too_soon"
    assert login.calls == 0


def test_quota_block_sets_state():
    store = FakeStore(mk_session(days_left=2, attempts_left=1))
    SessionRenewer(store=store, login_client=FakeLogin(),
                   credentials=creds).maybe_renew()
    assert RELOGIN_BLOCKED in store.states


# ---------------------------------------------------------------- 失败处理


def test_login_failure_does_not_raise():
    """★ 续期失败**不抛异常**：调用方可能是采集流程，不能因此中断。"""
    store = FakeStore(mk_session(days_left=2))
    r = SessionRenewer(store=store, login_client=FakeLogin(ok=False),
                       credentials=creds).maybe_renew()
    assert r.attempted is True
    assert r.ok is False
    assert RELOGIN_FAILED in store.states


def test_login_exception_does_not_raise():
    store = FakeStore(mk_session(days_left=2))
    r = SessionRenewer(store=store, login_client=FakeLogin(exc=OSError("网络")),
                       credentials=creds).maybe_renew()
    assert r.ok is False
    assert "网络" in r.message


def test_success_sets_ok_state():
    store = FakeStore(mk_session(days_left=2))
    r = SessionRenewer(store=store, login_client=FakeLogin(ok=True),
                       credentials=creds).maybe_renew()
    assert r.ok is True
    assert RELOGIN_OK in store.states


def test_missing_credentials_does_nothing():
    """没配用户名密码时不去登录（会白烧一次额度）。"""
    store = FakeStore(mk_session(days_left=2))
    login = FakeLogin()
    r = SessionRenewer(store=store, login_client=login,
                       credentials=lambda: ("", "")).maybe_renew()
    assert r.attempted is False
    assert r.reason == "no_credentials"
    assert login.calls == 0


def test_credentials_exception_handled():
    """★ 取凭据可能抛（主密钥坏了/解密失败），不能让它穿透。"""
    def boom():
        raise RuntimeError("解密失败")

    store = FakeStore(mk_session(days_left=2))
    r = SessionRenewer(store=store, login_client=FakeLogin(),
                       credentials=boom).maybe_renew()
    assert r.attempted is False
    assert r.reason == "no_credentials"


# ---------------------------------------------------------------- 结果形状


def test_result_ok_true_when_not_needed():
    """未续期不算失败——调用方不该把它当错误。"""
    store = FakeStore(mk_session(days_left=30))
    r = SessionRenewer(store=store, login_client=FakeLogin(),
                       credentials=creds).maybe_renew()
    assert r.ok is True


def test_result_to_dict():
    store = FakeStore(mk_session(days_left=30))
    r = SessionRenewer(store=store, login_client=FakeLogin(),
                       credentials=creds).maybe_renew()
    assert r.to_dict() == {
        "attempted": False, "ok": True,
        "reason": "not_needed", "message": "",
    }


def test_block_reason_code_and_message_agree():
    """★ 原因码与人话必须一致——两者分开算会漂移。

    `can_attempt_login` 现在只是把 `login_block_reason` 翻译成人话。
    这条钉住「码为 None ⇔ 允许」，以及两种码各自的文案特征。
    """
    from bt169.source.session import can_attempt_login, login_block_reason

    cases = [
        (None, None, True),
        (mk_session(days_left=30, attempts_left=5), None, True),
        (mk_session(days_left=2, attempts_left=1), "quota", False),
        (mk_session(days_left=2, attempts_left=0), "quota", False),
    ]
    for session, code, allowed in cases:
        assert login_block_reason(session) == code
        ok, msg = can_attempt_login(session)
        assert ok is allowed
        assert bool(msg) is (not allowed), "允许时不该有理由文案"

    # too_soon 单独构造（需要 last_login_attempt）
    recent = (datetime.now().astimezone() - timedelta(seconds=30)).isoformat()
    s = mk_session(days_left=2, attempts_left=5, last_attempt=recent)
    assert login_block_reason(s) == "too_soon"
    ok, msg = can_attempt_login(s)
    assert ok is False and "秒" in msg


def test_window_reset_unblocks_quota():
    """★ 服务端 900 秒后额度重置，本地守卫必须跟上。

    不重置的话「上次失败过」会永久堵住登录，而额度其实早回来了。
    """
    from bt169.source.session import login_block_reason

    old = (datetime.now().astimezone() - timedelta(seconds=1000)).isoformat()
    s = mk_session(days_left=2, attempts_left=1, last_attempt=old)
    assert login_block_reason(s) is None, "超窗口应视为已重置"


# ---------------------------------------------------------------- HTTP 接口


@pytest.fixture()
def renew_app(db, box, monkeypatch):
    """应用 + 假续期器。真实登录客户端会打网络，必须替换。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.api.routes import status as status_routes

    calls = {"n": 0}

    class FakeRenewer:
        def __init__(self, result):
            self._result = result

        def maybe_renew(self):
            calls["n"] += 1
            return self._result

    holder = {}

    def build(request):
        return holder["renewer"]

    monkeypatch.setattr(status_routes, "build_renewer", build)

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        c.db = db            # type: ignore[attr-defined]
        c.box = box          # type: ignore[attr-defined]
        c.holder = holder    # type: ignore[attr-defined]
        c.calls = calls      # type: ignore[attr-defined]
        c.FakeRenewer = FakeRenewer  # type: ignore[attr-defined]
        yield c


def test_renew_endpoint_ok(renew_app):
    from bt169.source.session import RenewResult

    renew_app.holder["renewer"] = renew_app.FakeRenewer(
        RenewResult(True, True, "renewed"))
    r = renew_app.post("/api/status/renew")
    assert r.status_code == 200, r.text
    assert r.json() == {"attempted": True, "ok": True,
                        "reason": "renewed", "message": ""}


def test_renew_endpoint_not_needed(renew_app):
    """★ 未到窗口时接口返回 attempted:false —— 不该消耗额度。"""
    from bt169.source.session import RenewResult

    renew_app.holder["renewer"] = renew_app.FakeRenewer(
        RenewResult(False, True, "not_needed"))
    body = renew_app.post("/api/status/renew").json()
    assert body["attempted"] is False and body["ok"] is True


def test_renew_endpoint_blocked(renew_app):
    from bt169.source.session import RenewResult

    renew_app.holder["renewer"] = renew_app.FakeRenewer(
        RenewResult(False, False, "quota", "额度仅剩 1 次"))
    body = renew_app.post("/api/status/renew").json()
    assert body["attempted"] is False
    assert body["ok"] is False
    assert body["reason"] == "quota"
    assert "额度" in body["message"]


def test_renew_endpoint_response_shape_is_closed(renew_app):
    """★ 响应形状是**封闭**的：只有四个固定键。

    续期路径手里有 Cookie（``ForumSession.cookies``），最容易的事故
    就是图省事把整个会话对象序列化出去。这条钉住「只有这四个键」——
    以后往 ``RenewResult`` 加字段时会被提醒重新想一遍该不该外发。
    """
    from bt169.source.session import RenewResult

    renew_app.holder["renewer"] = renew_app.FakeRenewer(
        RenewResult(True, True, "renewed"))
    body = renew_app.post("/api/status/renew").json()
    assert set(body) == {"attempted", "ok", "reason", "message"}


def test_status_endpoint_never_returns_cookies(db, box):
    """★ ``/api/status`` 只回派生信息，绝不回 Cookie 内容。"""
    from datetime import datetime, timedelta

    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.source.session import SessionStore

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        SessionStore(db).save_cookies(
            {"SlDj_2132_auth": "SUPER-SECRET-COOKIE"}, username="ymxh")
        r = c.get("/api/status")

    assert "SUPER-SECRET-COOKIE" not in r.text
    assert "cookies" not in r.json()["session"]


def test_status_exposes_renew_window(db, box):
    """``/api/status`` 要给出续期窗口，前端才能提示「N 天后自动续期」。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.source.session import RENEW_BEFORE_DAYS

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        body = c.get("/api/status").json()
    assert body["session"]["renew_window_days"] == RENEW_BEFORE_DAYS
