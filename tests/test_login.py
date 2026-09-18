"""登录模块测试。全部离线（用假 HTTP 客户端）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from bt169.db import Database
from bt169.source.login import LoginClient, LoginError, _parse_attempts_left
from bt169.source.session import (
    COOKIE_LIFETIME_DAYS,
    RENEW_BEFORE_DAYS,
    SessionStore,
)

LOGIN_HTML = """
<html><body>
<input type="hidden" name="formhash" value="abc123" />
<div id="main_messaqge_LIZWQ">
<div id="layer_login_LIZWQ">
<form method="post" name="login" id="loginform_LIZWQ"
      action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes&amp;loginhash=LIZWQ">
</form></div>
<span id="seccode_cSsU64R6"></span>
</div></body></html>
"""


class FakeForum:
    """假的 ForumClient，记录所有请求。"""

    def __init__(self, *, login_html: str = LOGIN_HTML,
                 check_result: bool = True, login_body: str = "",
                 image: bytes = b"\x89PNG-fake") -> None:
        self.base = "https://example.test"
        self._login_html = login_html
        self._check_result = check_result
        self._login_body = login_body
        self._image = image
        self.requests: list[str] = []
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.cookies: dict[str, str] = {}

    def get(self, path, *, headers=None, count_rate=True):  # type: ignore[no-untyped-def]
        self.requests.append(path)
        class R:
            text = ""
        r = R()
        if "action=login" in path and "loginsubmit" not in path:
            r.text = self._login_html
        elif "mod=seccode&action=check" in path:
            body = "succeed" if self._check_result else "invalid"
            r.text = f'<?xml version="1.0"?><root><![CDATA[{body}]]></root>'
        return r

    def get_bytes(self, path, *, headers=None):  # type: ignore[no-untyped-def]
        self.requests.append(path)
        return self._image

    def post(self, path, *, data, headers=None, count_rate=False):  # type: ignore[no-untyped-def]
        self.posts.append((path, data))
        self.cookies = {"SlDj_2132_sid": "newsid", "SlDj_2132_auth": "tok"}
        class R:
            text = ""
        r = R()
        r.text = self._login_body
        return r


@pytest.fixture()
def store(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield SessionStore(db)
    db.close()


# ------------------------------------------------------------ 额度文案解析


@pytest.mark.parametrize(
    "text,expected",
    [
        ("登录失败，您还可以尝试 4 次", 4),
        ("还可以尝试 0 次", 0),
        ("密码错误", None),
    ],
)
def test_parse_attempts_left(text, expected):
    assert _parse_attempts_left(text) == expected


# ------------------------------------------------------------ 登录表单解析


def test_login_form_extracts_both_hashes(store):
    """★ 回归：seccode idhash ≠ loginhash，必须分别提取。

    混用会导致 check 端点收到错的 idhash（静默 invalid）。
    """
    fc = FakeForum()
    lc = LoginClient(client=fc, store=store, solvers=[])  # type: ignore[arg-type]
    form = lc._load_form()
    assert form.formhash == "abc123"
    assert form.loginhash == "LIZWQ"
    assert form.idhash == "cSsU64R6"
    assert form.loginhash != form.idhash


def test_login_form_missing_formhash_raises(store):
    fc = FakeForum(login_html="<html>no form</html>")
    lc = LoginClient(client=fc, store=store, solvers=[])  # type: ignore[arg-type]
    with pytest.raises(LoginError, match="formhash"):
        lc._load_form()


def test_login_form_without_captcha(store):
    """IP 可信时 Discuz 会关掉验证码 → idhash 为空，不该崩。"""
    html = LOGIN_HTML.replace('<span id="seccode_cSsU64R6"></span>', "")
    fc = FakeForum(login_html=html)
    lc = LoginClient(client=fc, store=store, solvers=[])  # type: ignore[arg-type]
    assert lc._load_form().idhash == ""


# ------------------------------------------------------------ 登录成功


def test_login_success_saves_cookies(store):
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[欢迎您回来，会员]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("abcd")])  # type: ignore[arg-type]
    result = lc.login("user", "pass")

    assert result.ok is True
    assert result.captcha_images == 1

    saved = store.load()
    assert saved is not None
    assert saved.valid is True
    assert saved.cookies["SlDj_2132_sid"] == "newsid"
    assert saved.username == "user"


def test_login_submits_verified_captcha(store):
    """★ 提交的必须是**预校验通过**的码，而不是 OCR 的原始输出。"""
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[欢迎您回来]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("xyzw")])  # type: ignore[arg-type]
    lc.login("user", "pass")

    _, data = fc.posts[0]
    assert data["seccodeverify"] == "xyzw"
    assert data["seccodehash"] == "cSsU64R6"      # 是 idhash，不是 loginhash
    assert data["username"] == "user"
    assert data["cookietime"] == "2592000"
    assert data["questionid"] == "0"


def test_login_post_url_uses_loginhash(store):
    """★ 登录 POST 的 URL 用 loginhash（与 idhash 不同）。"""
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[欢迎您回来]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    lc.login("u", "p")
    assert "loginhash=LIZWQ" in fc.posts[0][0]
    assert "cSsU64R6" not in fc.posts[0][0]


def test_login_empty_credentials_raises(store):
    fc = FakeForum()
    lc = LoginClient(client=fc, store=store, solvers=[])  # type: ignore[arg-type]
    with pytest.raises(LoginError, match="不能为空"):
        lc.login("", "p")


def test_login_skips_captcha_when_no_idhash(store):
    """无验证码时不该调用 solver，也不该取图。"""
    from bt169.source.captcha import StubSolver

    html = LOGIN_HTML.replace('<span id="seccode_cSsU64R6"></span>', "")
    fc = FakeForum(login_html=html,
                   login_body="<root><![CDATA[欢迎您回来]]></root>")
    solver = StubSolver("never")
    lc = LoginClient(client=fc, store=store, solvers=[solver])  # type: ignore[arg-type]
    result = lc.login("u", "p")
    assert result.ok is True
    assert solver.calls == 0
    assert not any("mod=seccode" in r for r in fc.requests)


# ------------------------------------------------------------ 登录失败


def test_login_captcha_solve_failure(store):
    """6 张图都解不出 → 失败，且**不提交**登录（不消耗额度）。"""
    from bt169.source.captcha import StubSolver

    fc = FakeForum(check_result=False,
                   login_body="<root><![CDATA[欢迎您回来]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("bad")])  # type: ignore[arg-type]
    result = lc.login("u", "p")

    assert result.ok is False
    assert "验证码" in result.message
    assert fc.posts == []          # ★ 关键：一次都没提交
    assert result.captcha_images == 6


def test_login_quota_exhausted(store):
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[登录失败次数过多，请 15 分钟后再试]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    result = lc.login("u", "p")

    assert result.ok is False
    assert result.quota_exhausted is True
    assert result.attempts_left == 0


def test_login_records_attempts_left(store):
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[登录失败，您还可以尝试 3 次]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    result = lc.login("u", "p")

    assert result.ok is False
    assert result.attempts_left == 3
    saved = store.load()
    assert saved is not None and saved.login_attempts_left == 3
    assert saved.valid is False or saved.relogin_state == "failed"


def test_captcha_error_does_not_report_quota(store):
    """提交错误验证码不消耗额度 → 不得误报 attempts_left。"""
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[验证码填写错误]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    result = lc.login("u", "p")
    assert result.ok is False
    assert result.attempts_left is None
    assert result.quota_exhausted is False


# ------------------------------------------------------------ 会话存储


def test_session_store_empty_initially(store):
    assert store.load() is None


def test_session_save_and_load(store):
    s = store.save_cookies({"a": "1"}, username="u")
    assert s.valid is True
    assert s.cookies == {"a": "1"}
    loaded = store.load()
    assert loaded is not None and loaded.cookies == {"a": "1"}


def test_session_save_is_idempotent_upsert(store):
    store.save_cookies({"a": "1"}, username="u")
    store.save_cookies({"b": "2"}, username="u2")
    loaded = store.load()
    assert loaded is not None
    assert loaded.cookies == {"b": "2"} and loaded.username == "u2"


def test_session_expires_in_30_days(store):
    s = store.save_cookies({"a": "1"})
    assert s.expires_at is not None
    exp = datetime.fromisoformat(s.expires_at)
    delta = exp - datetime.now().astimezone()
    assert abs(delta.days - COOKIE_LIFETIME_DAYS) <= 1
    assert s.days_left is not None and s.days_left >= COOKIE_LIFETIME_DAYS - 1


def test_session_needs_renewal_window(store):
    """★ 主动续期：距失效 ≤5 天时进入续期窗口。"""
    s = store.save_cookies({"a": "1"})
    assert s.needs_renewal() is False        # 刚登录，30 天

    soon = (datetime.now().astimezone()
            + timedelta(days=RENEW_BEFORE_DAYS - 1)).isoformat(timespec="seconds")
    with store._db.write() as conn:
        conn.execute("UPDATE forum_session SET expires_at=? WHERE id=1", (soon,))
    loaded = store.load()
    assert loaded is not None and loaded.needs_renewal() is True


def test_session_mark_invalid(store):
    store.save_cookies({"a": "1"})
    store.mark_invalid()
    loaded = store.load()
    assert loaded is not None and loaded.valid is False


def test_session_corrupt_cookies_json(store):
    """坏 JSON 不该让整个加载崩掉。"""
    store.save_cookies({"a": "1"})
    with store._db.write() as conn:
        conn.execute("UPDATE forum_session SET cookies='{坏' WHERE id=1")
    loaded = store.load()
    assert loaded is not None and loaded.cookies == {}


def test_session_set_relogin_state(store):
    store.save_cookies({"a": "1"})
    store.set_relogin_state("retrying")
    loaded = store.load()
    assert loaded is not None and loaded.relogin_state == "retrying"


def test_session_record_attempt_keeps_previous_when_none(store):
    """COALESCE 语义：None 不该把已有额度清成 NULL。"""
    store.save_cookies({"a": "1"})
    store.record_attempt(attempts_left=4)
    store.record_attempt(attempts_left=None)
    loaded = store.load()
    assert loaded is not None and loaded.login_attempts_left == 4


# ------------------------------------------------------------ 响应文案清洗
#
# ★ 这些是**真实抓到的响应形状**（不是编的）。实测提交一次失败登录，
#   Discuz 返回的就是「文案 + 回调脚本」塞在同一个 CDATA 里。


REAL_FAIL_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<root><![CDATA[登录失败，您还可以尝试 4 次'
    '<script type="text/javascript" reload="1">'
    "if(typeof errorhandle_=='function') {"
    "errorhandle_('登录失败，您还可以尝试 4 次', {'loginperm':'4'});}"
    "</script>]]></root>"
)


def test_failed_login_message_has_no_script():
    """★ 回归：提示文案不能带 ``<script>``。

    实测用户看到的是「登录失败，您还可以尝试 4 次<script …>errorhandle(…)」
    这一整坨——CLI 打印、前端 toast 都会糊出来。
    """
    from bt169.source.login import LoginClient

    result = LoginClient._interpret(REAL_FAIL_BODY)
    assert result.ok is False
    assert "<script" not in result.message
    assert "errorhandle" not in result.message
    assert result.message == "登录失败，您还可以尝试 4 次"


def test_failed_login_still_parses_quota_from_dirty_body():
    """清洗后仍必须能解析出剩余额度（这是额度保护的关键输入）。"""
    from bt169.source.login import LoginClient

    result = LoginClient._interpret(REAL_FAIL_BODY)
    assert result.attempts_left == 4
    assert result.quota_exhausted is False


def test_clean_message_keeps_plain_text():
    from bt169.source.login import clean_message

    assert clean_message("  欢迎您回来  ") == "欢迎您回来"
    assert clean_message("a<b>c</b>") == "ac"
    assert clean_message("<script>x</script>好") == "好"


def test_quota_exhausted_message_cleaned():
    """额度耗尽的响应同样会带脚本——不能只清洗「还能尝试」那一支。"""
    from bt169.source.login import LoginClient

    body = (
        '<root><![CDATA[登录失败次数过多，请 15 分钟后再试'
        '<script type="text/javascript" reload="1">'
        "errorhandle_('登录失败次数过多', {'loginperm':'0'});</script>]]></root>"
    )
    result = LoginClient._interpret(body)
    assert result.quota_exhausted is True
    assert "<script" not in result.message
    assert result.message == "登录失败次数过多，请 15 分钟后再试"


# ------------------------------------------------------------ 额度保护
#
# ★ 这一组防的是**真实事故**：实测对着线上论坛提交了一次失败登录，
#   额度从 5 掉到 4。ARCHITECTURE.md §5.2 早就定了规则 2/3，
#   但代码里从来没检查过——文档写了不等于实现了。


def _session(**over):
    from bt169.source.session import ForumSession

    base = dict(
        cookies={}, username="u", obtained_at="2026-09-01T00:00:00+08:00",
        expires_at="2026-10-01T00:00:00+08:00", valid=True,
        login_attempts_left=None, last_login_attempt=None,
        last_relogin_at=None, relogin_state=None,
    )
    return ForumSession(**{**base, **over})


def _ago(seconds: float) -> str:
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() - timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def test_allows_login_when_no_session():
    from bt169.source.session import can_attempt_login

    assert can_attempt_login(None) == (True, "")


def test_blocks_when_quota_at_safety_margin():
    """★ 规则 2：只剩安全下限时拒绝（留 1 次救命）。"""
    from bt169.source.session import can_attempt_login

    allowed, reason = can_attempt_login(
        _session(login_attempts_left=1, last_login_attempt=_ago(700))
    )
    assert allowed is False
    assert "额度" in reason


def test_blocks_when_quota_exhausted():
    from bt169.source.session import can_attempt_login

    allowed, _ = can_attempt_login(
        _session(login_attempts_left=0, last_login_attempt=_ago(700))
    )
    assert allowed is False


def test_allows_when_quota_healthy():
    from bt169.source.session import can_attempt_login

    allowed, _ = can_attempt_login(
        _session(login_attempts_left=4, last_login_attempt=_ago(700))
    )
    assert allowed is True


def test_quota_resets_after_window():
    """★ 服务端 900 秒后计数自动重置——本地必须跟上。

    否则「上次失败过」会永久堵住登录，而实际额度早就回来了。
    """
    from bt169.source.session import can_attempt_login

    allowed, _ = can_attempt_login(
        _session(login_attempts_left=0, last_login_attempt=_ago(1000))
    )
    assert allowed is True, "超过 900 秒窗口后额度应视为已重置"


def test_blocks_rapid_resubmit():
    """★ 规则 3：距上次提交太近时拒绝（避免风控）。"""
    from bt169.source.session import can_attempt_login

    allowed, reason = can_attempt_login(
        _session(login_attempts_left=5, last_login_attempt=_ago(30))
    )
    assert allowed is False
    assert "风控" in reason or "再等" in reason


def test_login_refuses_when_quota_low(store):
    """★ 端到端：额度不足时**一次 HTTP 请求都不发**。

    这条最要紧——「拒绝」必须发生在提交之前，否则守卫毫无意义。
    """
    from bt169.source.login import LoginClient
    from bt169.source.session import RELOGIN_BLOCKED

    store.record_attempt(attempts_left=1, state="failed")
    fc = FakeForum(login_body="<root><![CDATA[欢迎您回来]]></root>")
    lc = LoginClient(client=fc, store=store, solvers=[])

    result = lc.login("u", "p")

    assert result.ok is False
    assert "额度" in result.message
    assert fc.posts == [], "额度不足时绝不该提交登录"
    assert store.load().relogin_state == RELOGIN_BLOCKED
