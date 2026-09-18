"""Telegram 转发接口（T-1~T-8）。

全部离线：把 ``build_client`` 换成假客户端，不打真实 Bot API。
"""

from __future__ import annotations

import json

import pytest

from bt169 import config
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
    """应用 + 假 TG 客户端。凭据写进设置。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.api.routes import telegram as tg_routes
    from bt169.telegram import TelegramClient

    http = FakeHTTP()
    monkeypatch.setattr(
        tg_routes, "build_client",
        lambda settings: TelegramClient(token="T", chat_id="C", http=http),
    )

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        c.http = http        # type: ignore[attr-defined]
        c.db = db            # type: ignore[attr-defined]
        c.box = box          # type: ignore[attr-defined]
        yield c


def set_tg(db, box):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "T")
    sr.put(config.section_key("tg", "chat_id"), "C")


# ---------------------------------------------------------------- 单帖转发


def test_forward_one_ok(tg_app):
    seed_post(tg_app.db, 101)
    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "message_id": 7}
    assert len(tg_app.http.calls) == 1


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
    assert len(tg_app.http.calls) == 1, "第二次不该真的发请求"


def test_forward_one_without_ed2k_returns_502(tg_app):
    """★ 未解锁帖不可转发（T-5）。"""
    seed_post(tg_app.db, 101, ed2k=None)
    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 502
    assert "ed2k" in r.json()["error"]["message"]


def test_forward_one_not_configured_returns_400(tg_app, monkeypatch):
    """★ 未配置 TG → 400 + tg_not_configured，前端据此引导去设置（T-4）。"""
    from bt169.api.routes import telegram as tg_routes
    from bt169.telegram import TelegramNotConfigured

    def boom(settings):
        raise TelegramNotConfigured("尚未配置 Telegram Bot Token 或 Chat ID")

    monkeypatch.setattr(tg_routes, "build_client", boom)
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


def test_forward_one_rate_limited_returns_429_with_retry_after(tg_app):
    body = json.dumps({"ok": False, "parameters": {"retry_after": 42}})
    tg_app.http.status = 429
    tg_app.http.body = body
    seed_post(tg_app.db, 101)

    r = tg_app.post("/api/posts/101/forward")
    assert r.status_code == 429
    detail = r.json()["error"]
    assert detail["code"] == "tg_rate_limited"
    assert detail["retry_after"] == 42


def test_forward_one_bot_error_returns_502(tg_app):
    tg_app.http.status = 500
    tg_app.http.body = "boom"
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
    from bt169.telegram import TelegramNotConfigured

    def boom(settings):
        raise TelegramNotConfigured("未配置")

    monkeypatch.setattr(tg_routes, "build_client", boom)
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


def test_settings_test_telegram_failure_returns_ok_false(tg_app):
    """★ 测试接口失败返回 200 + ok:false，不是 5xx。

    设置面板的「测试」按钮要**显示**失败原因，用错误状态码会让前端
    走通用错误分支、丢掉 detail。
    """
    tg_app.http.status = 401
    tg_app.http.body = '{"ok":false,"description":"Unauthorized"}'

    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Unauthorized" in body["detail"]


def test_settings_test_telegram_not_configured_ok_false(tg_app, monkeypatch):
    from bt169.api.routes import telegram as tg_routes
    from bt169.telegram import TelegramNotConfigured

    def boom(settings):
        raise TelegramNotConfigured("尚未配置")

    monkeypatch.setattr(tg_routes, "build_client", boom)
    r = tg_app.post("/api/settings/test/telegram")
    assert r.status_code == 200
    assert r.json()["ok"] is False
