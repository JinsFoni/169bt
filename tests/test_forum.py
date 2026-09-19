"""HTTP 客户端与限速测试。全部离线（httpx MockTransport）。"""

from __future__ import annotations

import httpx
import pytest

from bt169.source.forum import (
    FetchError,
    ForumClient,
    LoginRequired,
    RateLimiter,
)

LIST_HTML = """
<html><body><table>
<tbody id="normalthread_111">
  <tr><th><a class="xst" href="viewthread.php?tid=111">[4K] START-624 标题</a></th>
  <td class="by"><cite><span class="list_author">作者: <a>someone</a></span>
  <em><span title="2026-9-14">4 天前</span></em></cite></td>
  <td class="num"><span class="replynum">3</span></td></tr>
</tbody>
</table></body></html>
"""

DETAIL_HTML = """
<html><body><h1>START-624 标题</h1>
<td class="t_f">出演者：本庄鈴<br />ed2k://|file|a.mkv|1|AB|/</td>
</body></html>
"""


def make_client(handler, **kw):  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, follow_redirects=False)
    return ForumClient(client=inner, limiter=RateLimiter((0, 0)), **kw)


# ------------------------------------------------------------ 限速器


def test_rate_limiter_first_call_no_sleep():
    slept = []
    rl = RateLimiter((2.0, 5.0), sleep=slept.append)
    assert rl.wait() == 0.0
    assert slept == []


def test_rate_limiter_sleeps_within_range():
    slept = []
    rl = RateLimiter((2.0, 5.0), sleep=slept.append)
    rl.wait()
    for _ in range(20):
        delay = rl.wait()
        assert 2.0 <= delay <= 5.0
    assert len(slept) == 20
    assert all(2.0 <= d <= 5.0 for d in slept)


def test_rate_limiter_jitters():
    """★ 必须是随机抖动而非固定间隔——固定间隔本身就是可识别的爬虫特征。"""
    rl = RateLimiter((2.0, 5.0), sleep=lambda d: None)
    rl.wait()
    delays = {round(rl.wait(), 4) for _ in range(30)}
    assert len(delays) > 20


# ------------------------------------------------------------ 列表页


def test_list_page_parses_rows():
    fc = make_client(lambda req: httpx.Response(200, text=LIST_HTML))
    rows = fc.list_page(fid="192", page=1)
    assert len(rows) == 1
    assert rows[0].tid == 111
    assert rows[0].post_date == "2026-09-14"


def test_list_page_builds_correct_url():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, text=LIST_HTML)

    fc = make_client(handler)
    fc.list_page(fid="192", page=7)
    assert "mod=forumdisplay" in seen["url"]
    assert "fid=192" in seen["url"]
    assert "page=7" in seen["url"]


def test_user_agent_sent():
    """论坛拒绝默认 python UA，必须带浏览器 UA。"""
    seen = {}

    def handler(req):
        seen["ua"] = req.headers.get("user-agent", "")
        return httpx.Response(200, text=LIST_HTML)

    make_client(handler).list_page()
    assert "Mozilla" in seen["ua"]


# ------------------------------------------------------------ 详情页


def test_fetch_thread_parses():
    fc = make_client(lambda req: httpx.Response(200, text=DETAIL_HTML))
    d = fc.fetch_thread(999)
    assert d.tid == 999
    assert d.ed2k is not None
    assert d.actress == "本庄鈴"


def test_fetch_thread_login_wall_raises():
    html = '<form id="loginform_x">您需要登录</form>'
    fc = make_client(lambda req: httpx.Response(200, text=html))
    with pytest.raises(LoginRequired):
        fc.fetch_thread(1)


# ------------------------------------------------------------ 错误处理


def test_redirect_to_login_raises_login_required():
    """★ 302 跳登录页 = 会话失效，必须抛出可识别的异常。"""
    def handler(req):
        return httpx.Response(
            302, headers={"location": "member.php?mod=logging&action=login"}
        )

    fc = make_client(handler)
    with pytest.raises(LoginRequired, match="会话失效"):
        fc.list_page()


def test_other_redirect_raises_fetch_error():
    def handler(req):
        return httpx.Response(302, headers={"location": "https://elsewhere/"})

    fc = make_client(handler)
    with pytest.raises(FetchError, match="重定向"):
        fc.list_page()


def test_http_500_raises():
    fc = make_client(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(FetchError, match="500"):
        fc.list_page()


def test_network_error_wrapped():
    def handler(req):
        raise httpx.ConnectError("connection refused")

    fc = make_client(handler)
    with pytest.raises(FetchError, match="请求失败"):
        fc.list_page()


# ------------------------------------------------------------ Cookie


def test_cookies_are_sent():
    seen = {}

    def handler(req):
        seen["cookie"] = req.headers.get("cookie", "")
        return httpx.Response(200, text=LIST_HTML)

    fc = make_client(handler, cookies={"SlDj_2132_sid": "abc"})
    fc.list_page()
    assert "SlDj_2132_sid=abc" in seen["cookie"]


def test_cookies_property_returns_dict():
    fc = make_client(lambda req: httpx.Response(200, text=LIST_HTML),
                     cookies={"a": "1"})
    assert fc.cookies == {"a": "1"}


# ------------------------------------------------------------ 上下文管理


def test_context_manager_closes_own_client():
    fc = ForumClient(limiter=RateLimiter((0, 0)))
    with fc as f:
        assert f is fc
    assert fc.is_closed is True


def test_does_not_close_injected_client():
    """外部注入的 client 不该被我们关掉（调用方可能还要复用）。"""
    inner = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=LIST_HTML)))
    fc = ForumClient(client=inner, limiter=RateLimiter((0, 0)))
    fc.close()
    assert inner.get("https://example.test/").status_code == 200
    inner.close()


# ------------------------------------------------------------ 限速接入


def test_browsing_requests_are_rate_limited():
    """浏览请求必须走限速器。"""
    slept = []
    limiter = RateLimiter((2.0, 5.0), sleep=slept.append)
    inner = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text=LIST_HTML)))
    fc = ForumClient(client=inner, limiter=limiter)
    fc.list_page(page=1)
    fc.list_page(page=2)
    assert len(slept) == 1          # 第二次请求前睡了一次
    assert 2.0 <= slept[0] <= 5.0


def test_captcha_image_not_rate_limited():
    """★ 验证码取图不计入浏览限速（属登录流程，不是抓取）。"""
    slept = []
    limiter = RateLimiter((2.0, 5.0), sleep=slept.append)
    inner = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"\x89PNG")))
    fc = ForumClient(client=inner, limiter=limiter)
    fc.get_bytes("misc.php?mod=seccode&idhash=x")
    assert slept == []


def test_post_not_rate_limited_by_default():
    slept = []
    limiter = RateLimiter((2.0, 5.0), sleep=slept.append)
    inner = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, text="ok")))
    fc = ForumClient(client=inner, limiter=limiter)
    fc.post("plugin.php?id=thanksplugin:thanks", data={"tid": "1"})
    assert slept == []


# ------------------------------------------------- POST 重定向（PRG）


def test_post_follows_301_like_browser():
    """★ 感谢插件实测:首次感谢后发 **301** 跳回帖子页（PRG 模式）。

    浏览器会静默跟随;曾因把 301 当错误,感谢在服务端已成功、
    帖子却被误标 failed(2026-09-19,15 帖全军覆没)。
    """
    def handler(req):
        if req.method == "POST":
            return httpx.Response(
                301, headers={"location": "/forum.php?mod=viewthread&tid=1"})
        return httpx.Response(200, text="unlocked")

    fc = make_client(handler)
    page = fc.post("plugin.php?id=thanksplugin:thanks&action=thanks",
                   data={"tid": "1"})
    assert page.status == 200
    assert page.text == "unlocked"


def test_post_redirect_to_login_raises_login_required():
    """POST 重定向到登录页 = 会话失效,与 GET 同一套识别。"""
    def handler(req):
        return httpx.Response(
            302, headers={"location": "member.php?mod=logging&action=login"})

    fc = make_client(handler)
    with pytest.raises(LoginRequired, match="会话失效"):
        fc.post("plugin.php?id=thanksplugin:thanks", data={"tid": "1"})


def test_post_redirect_loop_raises():
    """重定向环 → 明确报错,不无限跟随。"""
    def handler(req):
        return httpx.Response(
            301, headers={"location": "/plugin.php?id=thanksplugin:thanks"})

    fc = make_client(handler)
    with pytest.raises(FetchError, match="超限"):
        fc.post("plugin.php?id=thanksplugin:thanks", data={"tid": "1"})


def test_post_strict_mode_keeps_non200_error():
    """follow_redirects=False（login inajax 用）:非 200 依旧报错。"""
    def handler(req):
        return httpx.Response(301, headers={"location": "/elsewhere/"})

    fc = make_client(handler)
    with pytest.raises(FetchError, match="重定向"):
        fc.post("member.php?mod=logging&action=login", data={},
                follow_redirects=False)
