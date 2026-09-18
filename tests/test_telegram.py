"""Telegram 转发（P6 / 需求 T-1~T-8）。

全部离线：用假的 HTTP transport，不打真实 Bot API。
"""

from __future__ import annotations

import json

import pytest

from bt169.db import Database
from bt169.models import Post
from bt169.repo.posts import PostRepo
from bt169.telegram import (
    AlreadySent,
    RateLimited,
    TelegramClient,
    TelegramError,
    TelegramNotConfigured,
    build_message,
)


# ---------------------------------------------------------------- 假 transport


class FakeHTTP:
    """可编程的假 HTTP 客户端。记录所有请求，可注入响应/异常。"""

    def __init__(self, *, status: int = 200, body: str | None = None,
                 raise_exc: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, data=None, timeout=None):  # type: ignore[no-untyped-def]
        self.calls.append((url, json or data or {}))
        if self.raise_exc is not None:
            raise self.raise_exc

        class R:
            pass

        r = R()
        r.status_code = self.status
        if self.body is not None:
            r.text = self.body
        else:
            r.text = '{"ok":true,"result":{"message_id":42}}'
        r.json = lambda: __import__("json").loads(r.text)
        return r


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


@pytest.fixture()
def posts(db):
    return PostRepo(db)


def seed(posts: PostRepo, tid: int = 101, *, ed2k: str | None = "ed2k://|file|a.mkv|1|AB|/",
         code: str = "ABC-101", status: str = "done") -> None:
    posts.upsert_collected(
        tid=tid, title=f"标题 {tid}", code=code, actress="某人",
        release_date="2026-09-17", size="7GB",
        cover_img=None, detail_img=None, ed2k=ed2k,
        post_date="2026-09-14", status=status,
    )


# ------------------------------------------------------------------ 消息构造


def test_build_message_contains_code_and_ed2k():
    """★ 消息必须带上番号与 ed2k——Bot 侧靠番号命名文件。"""
    p = Post(tid=101, title="t", code="ABC-101", actress="某人",
             release_date="2026-09-17", size="7GB", cover_img=None,
             detail_img=None, cover_local=None, detail_local=None,
             ed2k="ed2k://|file|a.mkv|1|AB|/", post_date="2026-09-14",
             post_time=None, status="done", retry_count=0, last_error=None,
             next_retry_at=None, tg_sent_at=None, emby_status=None,
             emby_item_id=None, emby_checked=None,
             created_at="", updated_at="")
    msg = build_message(p)
    assert "ABC-101" in msg
    assert "ed2k://|file|a.mkv|1|AB|/" in msg
    # ★ ed2k 必须**独占一行**：Bot 侧解析器按行取链接，混在文字里会解析失败
    assert "ed2k://|file|a.mkv|1|AB|/" in msg.split("\n")


def test_build_message_without_ed2k_raises():
    """无 ed2k 的帖不该被转发（需求 T-5）。"""
    p = Post(tid=101, title="t", code="ABC-101", actress=None,
             release_date=None, size=None, cover_img=None, detail_img=None,
             cover_local=None, detail_local=None, ed2k=None,
             post_date="2026-09-14", post_time=None, status="pending",
             retry_count=0, last_error=None, next_retry_at=None,
             tg_sent_at=None, emby_status=None, emby_item_id=None,
             emby_checked=None, created_at="", updated_at="")
    with pytest.raises(TelegramError):
        build_message(p)


# ------------------------------------------------------------------ 客户端


def test_send_message_posts_to_correct_url_and_payload():
    http = FakeHTTP()
    c = TelegramClient(token="123:ABC", chat_id="456", http=http)
    mid = c.send_message("你好")

    assert mid == 42
    url, payload = http.calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload["chat_id"] == "456"
    assert payload["text"] == "你好"
    # ★ 必须关闭链接预览：ed2k 不是 URL，但预览尝试会让消息延迟
    assert payload["disable_web_page_preview"] is True


def test_send_message_does_not_use_parse_mode():
    """★ 不能设 parse_mode：ed2k 里的 ``|`` ``_`` 会被当标记语法，
    Telegram 会报 "can't parse entities" 而整条消息发不出去。"""
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)
    c.send_message("ed2k://|file|a_b.mkv|1|AB|/")
    _, payload = http.calls[0]
    assert "parse_mode" not in payload


def test_missing_token_or_chat_raises_not_configured():
    """需求 T-4：未配置时不该发无效请求。"""
    for token, chat in (("", "1"), ("t", ""), ("", "")):
        with pytest.raises(TelegramNotConfigured):
            TelegramClient(token=token, chat_id=chat, http=FakeHTTP())


def test_send_message_429_raises_rate_limited_with_retry_after():
    body = json.dumps({
        "ok": False, "error_code": 429,
        "description": "Too Many Requests: retry after 30",
        "parameters": {"retry_after": 30},
    })
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=429, body=body))
    with pytest.raises(RateLimited) as ei:
        c.send_message("x")
    assert ei.value.retry_after == 30


def test_send_message_429_without_parameters_still_raises():
    """★ 429 但响应体没有 retry_after → 仍须抛 RateLimited（retry_after=0）。

    不能因为解析不出数字就把它当普通错误——调用方靠异常**类型**决定
    是否停手，类型错了会继续发，把限流撞得更狠。
    """
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=429, body="<html>429</html>"))
    with pytest.raises(RateLimited) as ei:
        c.send_message("x")
    assert ei.value.retry_after == 0


def test_send_message_http_error_raises():
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=500, body="boom"))
    with pytest.raises(TelegramError) as ei:
        c.send_message("x")
    assert "500" in str(ei.value)


def test_send_message_ok_false_raises_with_description():
    body = json.dumps({"ok": False, "description": "chat not found"})
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=200, body=body))
    with pytest.raises(TelegramError) as ei:
        c.send_message("x")
    assert "chat not found" in str(ei.value)


def test_send_message_network_exception_wrapped():
    """★ 网络异常必须包成 TelegramError，而不是让 httpx 的异常穿透。

    需求 T-8：TG 不可达不得影响采集与浏览——上层只该处理 TelegramError。
    """
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(raise_exc=OSError("网络不可达")))
    with pytest.raises(TelegramError) as ei:
        c.send_message("x")
    assert "网络不可达" in str(ei.value)


def test_send_message_non_json_body_raises():
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=200, body="<html>not json</html>"))
    with pytest.raises(TelegramError):
        c.send_message("x")


# ------------------------------------------------------------------ 幂等


def test_forward_post_marks_sent(posts):
    """成功后必须写 ``tg_sent_at``（需求 T-7）。"""
    from bt169.telegram import forward_post

    seed(posts, 101)
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)
    forward_post(posts.get(101), client=c, posts_repo=posts)

    assert posts.get(101).tg_sent_at is not None
    assert len(http.calls) == 1


def test_forward_post_twice_raises_already_sent(posts):
    """★ 幂等：重复点「下载」不该重复发送（需求 T-7）。"""
    from bt169.telegram import forward_post

    seed(posts, 101)
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)
    forward_post(posts.get(101), client=c, posts_repo=posts)

    with pytest.raises(AlreadySent):
        forward_post(posts.get(101), client=c, posts_repo=posts)

    assert len(http.calls) == 1, "第二次不该真的发请求"


def test_forward_post_without_ed2k_raises(posts):
    """需求 T-5：未解锁帖不可转发。"""
    from bt169.telegram import forward_post

    seed(posts, 101, ed2k=None, status="pending")
    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP())
    with pytest.raises(TelegramError):
        forward_post(posts.get(101), client=c, posts_repo=posts)
    assert posts.get(101).tg_sent_at is None


def test_forward_post_does_not_mark_on_failure(posts):
    """★ 发送失败绝不能写 ``tg_sent_at``——否则帖永远发不出去了。"""
    from bt169.telegram import forward_post

    seed(posts, 101)
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=500, body="boom"))
    with pytest.raises(TelegramError):
        forward_post(posts.get(101), client=c, posts_repo=posts)
    assert posts.get(101).tg_sent_at is None, "失败不该标记为已发送"


# ------------------------------------------------------------------ 批量


def test_forward_many_sends_all_serially_with_interval(posts):
    """★ 串行 + 间隔：并发会触发 429（架构 §5.5.3）。"""
    from bt169.telegram import forward_many

    for tid in (101, 102, 103):
        seed(posts, tid)
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)
    slept: list[float] = []

    result = forward_many(
        [posts.get(t) for t in (101, 102, 103)],
        client=c, posts_repo=posts,
        interval=3.0, sleep=slept.append,
    )

    assert result["sent"] == 3
    assert result["failed"] == 0
    assert len(http.calls) == 3
    # 3 条之间只该睡 2 次（首条不睡）
    assert slept == [3.0, 3.0]


def test_forward_many_skips_already_sent(posts):
    from bt169.telegram import forward_many

    seed(posts, 101)
    seed(posts, 102)
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)
    forward_many([posts.get(101)], client=c, posts_repo=posts,
                 interval=0, sleep=lambda _: None)

    result = forward_many([posts.get(101), posts.get(102)],
                          client=c, posts_repo=posts,
                          interval=0, sleep=lambda _: None)
    assert result["skipped"] == 1
    assert result["sent"] == 1


def test_forward_many_continues_after_single_failure(posts):
    """★ 单条失败不中断整批（需求 T-6：可逐条重试）。"""
    from bt169.telegram import forward_many

    seed(posts, 101, ed2k=None, status="pending")   # 无 ed2k → 必然失败
    seed(posts, 102)
    seed(posts, 103)
    http = FakeHTTP()
    c = TelegramClient(token="t", chat_id="1", http=http)

    result = forward_many([posts.get(t) for t in (101, 102, 103)],
                          client=c, posts_repo=posts,
                          interval=0, sleep=lambda _: None)

    assert result["failed"] == 1
    assert result["sent"] == 2, "一条失败不该让后面的都失败"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["tid"] == 101


def test_forward_many_stops_on_rate_limit(posts):
    """★ 撞上限流必须**停手**，把剩余帖如实报失败。

    继续发只会越撞越久（retry_after 递增）。宁可让用户稍后重试。
    """
    from bt169.telegram import forward_many

    for tid in (101, 102, 103):
        seed(posts, tid)
    body = json.dumps({"ok": False, "parameters": {"retry_after": 60}})
    c = TelegramClient(token="t", chat_id="1",
                       http=FakeHTTP(status=429, body=body))

    result = forward_many([posts.get(t) for t in (101, 102, 103)],
                          client=c, posts_repo=posts,
                          interval=0, sleep=lambda _: None)

    assert result["sent"] == 0
    assert result["failed"] == 3
    assert "60" in result["errors"][0]["error"]


def test_forward_many_empty_list(posts):
    from bt169.telegram import forward_many

    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP())
    result = forward_many([], client=c, posts_repo=posts, interval=0,
                          sleep=lambda _: None)
    assert result == {"sent": 0, "skipped": 0, "failed": 0, "errors": []}


# ------------------------------------------------------- 基址可配置


def test_base_defaults_to_api_base():
    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP())
    assert c.base == "https://api.telegram.org"


def test_base_can_be_overridden():
    """★ 允许指向自建反代（api.telegram.org 在部分网络下不可达）。"""
    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP(),
                       base="http://127.0.0.1:8898")
    http = c.http
    c.send_message("x")
    assert http.calls[0][0].startswith("http://127.0.0.1:8898/bot")


def test_base_override_not_baked_at_class_definition(monkeypatch):
    """★ 回归守卫：``base`` 的默认值**不能**是数据类默认参数。

    数据类默认值在**类定义时**求值。若写成 ``base: str = API_BASE``，
    那么模块级 ``API_BASE`` 改了（测试注入、或将来做成可配置项）
    也不会带动它——默认实例会一直打到真实 api.telegram.org。
    这条测试就是钉住这个坑：改模块级常量后，**默认构造**的实例必须跟着变。
    """
    monkeypatch.setattr("bt169.telegram.API_BASE", "http://example.invalid")
    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP())
    assert c.base == "http://example.invalid"


def test_base_trailing_slash_stripped():
    """★ 用户把基址填成 ``https://x/`` 时不能拼出 ``//bot``。"""
    c = TelegramClient(token="t", chat_id="1", http=FakeHTTP(),
                       base="http://127.0.0.1:8898/")
    c.send_message("x")
    assert "//bot" not in c.http.calls[0][0]


def test_api_base_env_override(monkeypatch):
    """★ api.telegram.org 在部分网络下不可达，必须能指向自建反代。

    模块级常量在 import 时求值，所以这条只能测「读了环境变量」这个事实
    （用子进程重载模块才能真正验证）。这里直接验证当前值来自环境。
    """
    import importlib
    import bt169.telegram as tg

    monkeypatch.setenv("BT169_TG_API_BASE", "http://proxy.local:8080")
    importlib.reload(tg)
    try:
        assert tg.API_BASE == "http://proxy.local:8080"
        c = tg.TelegramClient(token="t", chat_id="1", http=FakeHTTP())
        assert c.base == "http://proxy.local:8080"
    finally:
        monkeypatch.delenv("BT169_TG_API_BASE", raising=False)
        importlib.reload(tg)
