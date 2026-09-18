"""感谢解锁测试。全离线（httpx MockTransport）。"""

from __future__ import annotations

import httpx
import pytest

from bt169.source.forum import ForumClient, LoginRequired, RateLimiter
from bt169.source.parse import extract_formhash
from bt169.source.thanks import ThanksClient, ThanksError

TID = 3986000
ED2K = "ed2k://|file|s169bbs.com@START-624_[4K].mkv|7515146184|0B17E95C|/"

# ---------------------------------------------------------------- 页面样本


def page_locked() -> str:
    """需要感谢才能看到内容。"""
    return f"""<html><body>
    <form id="mods">
      <input type="hidden" name="formhash" value="FH12345">
    </form>
    <div class="locked">本帖隐藏的内容需要感谢作者后才能显示</div>
    </body></html>"""


def page_unlocked() -> str:
    """感谢后（或已感谢过）：ed2k 可见。"""
    return f"""<html><body>
    <form id="mods">
      <input type="hidden" name="formhash" value="FH12345">
    </form>
    <div class="locked">本帖在感謝作者後顯示的內容</div>
    <div class="attach">{ED2K}</div>
    </body></html>"""


def page_no_thanks_no_ed2k() -> str:
    return """<html><body>
    <form><input type="hidden" name="formhash" value="FH12345"></form>
    <div>普通的帖子正文，没有附件也没有感谢按钮</div>
    </body></html>"""


def page_login_wall() -> str:
    return """<html><body>
    <form id="loginform_ABC123" method="post"
          action="member.php?mod=logging&amp;action=login&amp;loginsubmit=yes">
      <input type="hidden" name="formhash" value="FH999">
    </form>
    </body></html>"""


# ---------------------------------------------------------------- 脚手架


class Recorder:
    """按 URL 关键字分派的假论坛，记录所有请求。"""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        data = {}
        if request.content:
            from urllib.parse import parse_qs

            # ★ keep_blank_values：``saying=`` 是空串，默认会被丢弃，
            #   导致断言 ``data["saying"] == ""`` 抛 KeyError。
            data = {
                k: v[0]
                for k, v in parse_qs(
                    request.content.decode(), keep_blank_values=True
                ).items()
            }
        self.calls.append((request.method, url, data))

        for key, value in self.routes.items():
            if key in url:
                if callable(value):
                    return value(request)
                return httpx.Response(200, text=str(value))
        return httpx.Response(404, text="not found")

    @property
    def posts(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == "POST"]


def make_client(routes: dict[str, object]) -> tuple[ForumClient, Recorder]:
    rec = Recorder(routes)
    inner = httpx.Client(transport=httpx.MockTransport(rec.handler))
    fc = ForumClient(client=inner, limiter=RateLimiter((0.0, 0.0)))
    return fc, rec


# ---------------------------------------------------------------- 已解锁


def test_already_unlocked_makes_no_post():
    """★ 已能直接看到 ed2k → 一次 POST 都不该发。"""
    fc, rec = make_client({"viewthread": page_unlocked()})
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is True
    assert r.ed2k == ED2K
    assert rec.posts == [], "不该发感谢请求"


# ---------------------------------------------------------------- 首次感谢


def test_thank_success_unlocks_ed2k():
    state = {"thanked": False}

    def viewthread(request):
        return httpx.Response(200, text=page_unlocked() if state["thanked"]
                              else page_locked())

    def thanks(request):
        state["thanked"] = True
        return httpx.Response(200, text="感謝作者，現在您可以看到隱藏內容")

    fc, rec = make_client({"thanksplugin": thanks, "viewthread": viewthread})
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is True
    assert r.ed2k == ED2K
    assert r.already is False
    assert len(rec.posts) == 1

    method, url, data = rec.posts[0]
    assert "thanksplugin:thanks" in url
    assert "action=thanks" in url
    assert data["tid"] == str(TID)
    assert data["formhash"] == "FH12345"
    assert data["thanksubmit"] == "true"
    assert data["saying"] == ""


def test_thank_repeat_is_idempotent():
    """★ 重复感谢返回「已感謝過了」，但页面同样能看到 ed2k → 视为成功。

    真实路径：首次 GET 仍是锁定页（例如本地没记「已感谢」状态），
    POST 得到「已感謝過了」，再 GET 就能看到 ed2k。
    """
    calls = {"n": 0}

    def viewthread(request):
        calls["n"] += 1
        return httpx.Response(200, text=page_locked() if calls["n"] == 1
                              else page_unlocked())

    def thanks(request):
        return httpx.Response(200, text="您已經感謝過作者了")

    fc, rec = make_client({"thanksplugin": thanks, "viewthread": viewthread})
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is True
    assert r.already is True
    assert r.ed2k == ED2K
    assert len(rec.posts) == 1


def test_thank_uses_formhash_from_current_page():
    """★ formhash 必须来自详情页，不能跨页复用。"""
    calls = {"n": 0}

    def viewthread(request):
        calls["n"] += 1
        html = page_locked() if calls["n"] == 1 else page_unlocked()
        return httpx.Response(200, text=html.replace("FH12345", "PAGE_FH"))

    def thanks(request):
        return httpx.Response(200, text="ok")

    fc, rec = make_client({"thanksplugin": thanks, "viewthread": viewthread})
    ThanksClient(client=fc).thank(TID)

    assert rec.posts[0][2]["formhash"] == "PAGE_FH"


# ---------------------------------------------------------------- 失败路径


def test_thank_without_ed2k_after_post_fails():
    """感谢已提交，但页面始终不出 ed2k → 失败。"""
    fc, _ = make_client({
        "thanksplugin": "感谢成功",
        "viewthread": page_locked(),      # 感谢后页面仍无 ed2k
    })
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is False
    assert r.ed2k is None


def test_already_thanked_but_still_no_ed2k():
    """已感谢过却仍无 ed2k → 可能是「回复可见」而非「感谢可见」。"""
    fc, _ = make_client({
        "thanksplugin": "您已經感謝過作者了",
        "viewthread": page_locked(),
    })
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is False
    assert r.already is True
    assert "未出现 ed2k" in r.message


def test_page_without_thanks_button():
    """既无 ed2k 也无感谢按钮 → 不发 POST，直接失败。"""
    fc, rec = make_client({"viewthread": page_no_thanks_no_ed2k()})
    r = ThanksClient(client=fc).thank(TID)

    assert r.ok is False
    assert rec.posts == []


def test_missing_formhash_raises():
    html = page_locked().replace('name="formhash" value="FH12345"', "")
    fc, _ = make_client({"viewthread": html})
    with pytest.raises(ThanksError, match="formhash"):
        ThanksClient(client=fc).thank(TID)


def test_login_wall_raises_login_required():
    """★ 会话失效要抛出可中止任务的异常，而不是当成普通失败。"""
    fc, rec = make_client({"viewthread": page_login_wall()})
    with pytest.raises(LoginRequired):
        ThanksClient(client=fc).thank(TID)
    assert rec.posts == []


def test_thread_gone_raises():
    fc, _ = make_client({
        "thanksplugin": "指定的主题不存在或已被删除",
        "viewthread": page_locked(),
    })
    with pytest.raises(ThanksError, match="不存在"):
        ThanksClient(client=fc).thank(TID)


# ---------------------------------------------------------------- formhash 解析


def test_extract_formhash():
    assert extract_formhash(page_locked()) == "FH12345"
    assert extract_formhash("<html></html>") is None


def test_extract_formhash_handles_attr_order():
    """属性顺序不固定（value 在前也要能取到）。"""
    html = '<input type="hidden" value="REVERSED" name="formhash">'
    assert extract_formhash(html) == "REVERSED"


def test_extract_formhash_tolerates_extra_attrs():
    html = '<input class="x" name="formhash" id="fh" value="WITHATTRS">'
    assert extract_formhash(html) == "WITHATTRS"
