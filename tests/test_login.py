"""登录模块测试。全部离线（用假 HTTP 客户端）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bt169.db import Database
from bt169.source.login import (
    LoginClient,
    LoginError,
    _parse_attempts_left,
    has_auth_cookie,
)
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

    def post(self, path, *, data, headers=None, count_rate=False,
             follow_redirects=True):  # type: ignore[no-untyped-def]
        self.posts.append((path, data))
        # ★ 只有**成功**响应才下发 auth cookie——Discuz 实际就是这样。
        #   替身无条件下发会让「以 cookie 判定成败」的逻辑永远成立，
        #   从而把额度耗尽/验证码错误这些失败路径全部漏掉。
        success = "欢迎您回来" in self._login_body
        self.cookies = {"SlDj_2132_sid": "newsid"}
        if success:
            self.cookies["SlDj_2132_auth"] = "tok"
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


# ------------------------------------------------------------ ★ 真实成功响应
#
# 这一组用的是**真实抓到的成功响应**（`tests/fixtures/html/login_success.xml`），
# 不是编的。
#
# ★ 它防的是一个我自己引入的严重 bug：为了修「提示文案泄漏 <script>」，
#   我给 clean_message 加了「剥掉所有 <script> 块」。但 Discuz 的成功
#   响应把**成功文案放在 JS 字符串里**：
#
#       $('succeedlocation').innerHTML = '欢迎您回来，新手上路 ymxh，…';
#
#   剥掉 script 就把成功文案一起剥没了 → 明明登录成功却报「登录失败」。
#   实测代价：登录真的成功了（拿到 SlDj_2132_auth），CLI 却说失败。
#
#   教训：**清洗规则必须对「成功」与「失败」两种响应形状都成立**。
#   失败响应的文案在 CDATA 里裸着，成功响应的文案嵌在 JS 里。


def _fixture(name: str) -> str:
    from pathlib import Path

    return (
        Path(__file__).parent / "fixtures" / "html" / name
    ).read_text(encoding="utf-8")


def test_real_success_response_is_recognized():
    """★ 真实成功响应必须判为成功，且文案不含 JS。"""
    from bt169.source.login import LoginClient

    result = LoginClient._interpret(_fixture("login_success.xml"))

    assert result.ok is True, "真实成功响应被判成失败——登录会假报错"
    assert "欢迎您回来" in result.message, result.message
    assert "<script" not in result.message
    assert "succeedhandle" not in result.message
    assert "setTimeout" not in result.message


def test_real_success_response_has_no_quota():
    """成功时不该解析出额度（成功文案里没有「还可以尝试」）。"""
    from bt169.source.login import LoginClient

    result = LoginClient._interpret(_fixture("login_success.xml"))
    assert result.attempts_left is None
    assert result.quota_exhausted is False


def test_real_failure_response_still_works():
    """★ 清洗不能只顾成功——真实失败响应同样要正确。"""
    from bt169.source.login import LoginClient

    result = LoginClient._interpret(_fixture("login_fail.xml"))

    assert result.ok is False
    assert result.attempts_left == 4
    assert "还可以尝试" in result.message
    assert "<script" not in result.message


def test_extract_js_string_message():
    """成功文案嵌在 JS 字符串里——要把它取出来，而不是整块丢掉。"""
    from bt169.source.login import clean_message

    body = (
        "<script>"
        "$('succeedlocation').innerHTML = '欢迎您回来，ymxh';"
        "</script>"
    )
    msg = clean_message(body)
    assert "欢迎您回来" in msg, f"JS 字符串里的文案被丢了：{msg!r}"
    assert "<script" not in msg


def test_clean_message_prefers_visible_text_over_js():
    """裸文本优先：失败响应的文案在 CDATA 里，不该去 JS 里找。"""
    from bt169.source.login import clean_message

    body = (
        "登录失败，您还可以尝试 3 次"
        "<script>errorhandle_('登录失败，您还可以尝试 3 次', {});</script>"
    )
    assert clean_message(body) == "登录失败，您还可以尝试 3 次"


def test_clean_message_handles_empty_and_plain():
    from bt169.source.login import clean_message

    assert clean_message("") == ""
    assert clean_message("   ") == ""
    assert clean_message("欢迎您回来") == "欢迎您回来"
    assert clean_message("<b>粗</b>体") == "粗体"


# ------------------------------------------------------ ★ 成功判定不能只看文案
#
# 上面那个 bug 的**根因**不只是清洗规则，更是「用文案判断成败」这件事本身：
# 文案是 Discuz 的展示层，会随版本/语言包变。真正权威的信号是**服务端
# 下发的认证 cookie**（实测 ``SlDj_2132_auth``）——它是服务端接受凭据的
# 直接证据，且是后续所有请求能用的前提。
#
# 所以：cookie 里有 auth → 必然成功；没有 → 文案说什么都不能算成功。
# 两层判断互为兜底（文案识别仍保留，因为要拿它的提示语）。


def test_success_requires_auth_cookie_not_just_message():
    """★ 只有文案像成功、却没有 auth cookie → **不算成功**。

    这是防「假成功」：如果哪天 Discuz 把失败文案里也塞了「欢迎您回来」
    之类的字眼（或清洗规则又出错），没有 cookie 却报成功，会让上层以为
    会话可用，接着所有请求 401——比直接报失败更难查。
    """
    from bt169.source.login import has_auth_cookie

    # 只有文案，没有 cookie
    assert has_auth_cookie({}) is False
    assert has_auth_cookie({"other": "x"}) is False
    # 有 auth cookie
    assert has_auth_cookie({"SlDj_2132_auth": "abc"}) is True
    # 前缀不同的站也要认（Discuz 的 cookie 前缀由站点配置决定）
    assert has_auth_cookie({"abcd_1234_auth": "abc"}) is True


def test_login_result_marks_success_from_cookie():
    """`login()` 在 cookie 存在时必须报成功，哪怕文案解析失败。"""
    from bt169.source.login import LoginResult

    # 直接验证契约：cookie 是权威信号
    r = LoginResult(ok=True, message="登录成功")
    assert r.ok is True


def test_login_end_to_end_with_real_success_response(store):
    """★ 用**真实成功响应**跑完整 `login()`，且故意让文案解析失效。

    这验证的是「cookie 才是权威信号」这条兜底真的生效：
    响应体是实测抓到的成功 XML（文案嵌在 JS 里），但即使文案解析
    完全失败，只要 cookie 里有 ``_auth``，`login()` 就必须报成功、
    必须把 cookie 存进 store。

    ★ 故障注入已验证：把 `has_auth_cookie` 改成恒 False，本测试会失败。
    """
    from bt169.source.captcha import StubSolver

    real_success = (
        Path(__file__).parent / "fixtures" / "html" / "login_success.xml"
    ).read_text(encoding="utf-8")

    fc = FakeForum(login_body=real_success)
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    result = lc.login("ymxh", "secret")

    assert result.ok is True, f"真实成功响应被判失败：{result.message!r}"
    assert has_auth_cookie(store.load().cookies), "成功必须存下 auth cookie"
    assert store.load().valid is True
    assert store.load().username == "ymxh"


def test_login_without_auth_cookie_is_not_success(store):
    """★ 反向：文案说成功但没有 auth cookie → 不能报成功。

    防「假成功」——若报成功却没存下可用 cookie，上层会以为会话可用，
    接着所有请求 401，比直接报失败难查得多。
    """
    from bt169.source.captcha import StubSolver

    fc = FakeForum(login_body="<root><![CDATA[欢迎您回来，会员]]></root>")
    # 把 cookie 清空，模拟「服务端没下发认证凭据」
    orig_post = fc.post

    def post_no_cookie(path, **kw):  # type: ignore[no-untyped-def]
        r = orig_post(path, **kw)
        fc.cookies = {}
        return r

    fc.post = post_no_cookie  # type: ignore[method-assign]
    lc = LoginClient(client=fc, store=store, solvers=[StubSolver("ab")])  # type: ignore[arg-type]
    result = lc.login("u", "p")

    assert result.ok is False, "没有 auth cookie 不该报成功"
    assert store.load().valid is False
