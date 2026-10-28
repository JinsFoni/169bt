"""LTX 接口（``/api/ltx/*``）：浏览、同步、封面、转发。

全部离线：同步用假 ``LtxSource``，转发用假 MTProto 发送器。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bt169 import config
from bt169.api.app import create_app
from bt169.api.routes import ltx as ltx_routes
from bt169.avdb import AvdbAuthError, AvdbError, AvdbNotConfigured
from bt169.collector.imagecache import CARD_WIDTH, LIGHTBOX_WIDTH
from bt169.repo.ltx import LtxRepo
from bt169.repo.posts import now_iso


# ---------------------------------------------------------------- 夹具


def row(tid, *, date="2026-10-25", number="SSIS-157", rejected=0,
        created="2026-10-25T12:00:00+08:00", ed2k="ed2k://|file|x.mp4|1|AB|/"):
    return {
        "id": tid,
        "number": number,
        "title": f"标题 {tid}",
        "section": "LTX破解",
        "publish_date": date,
        "size_bytes": 3607826070,
        "ed2k": ed2k,
        "cover_url": "https://img/c.jpg",
        "detail_url": "https://javdb.com/v/x",
        "rejected": rejected,
        "created_at": created,
    }


class FakeMtp:
    """替身 MtpSender 所需的最小客户端（与 test_api_telegram 同形状）。"""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def connect(self):
        pass

    def disconnect(self):
        pass

    def send_message(self, entity, text):
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc

        class M:
            id = 42
        return M()


@pytest.fixture()
def app(db, box, monkeypatch):
    """应用 + 假发送器（转发链路走 MTProto 方案 A）。"""
    from bt169.mtpproto import MtpSender

    fake = FakeMtp()
    monkeypatch.setattr(
        ltx_routes, "get_sender",
        lambda settings: MtpSender(client=fake, target="@nan_share_bot"),
    )
    # 批量转发默认每条间隔 3 秒（TG 限流），测试里把间隔压到 0
    from bt169 import telegram as tg

    monkeypatch.setattr(
        ltx_routes, "forward_ltx_many",
        lambda items, *, client, ltx_repo: tg.forward_ltx_many(
            items, client=client, ltx_repo=ltx_repo,
            interval=0, sleep=lambda _s: None,
        ),
    )

    application = create_app(db, box=box, ui_dir=None, image_dir=None)
    with TestClient(application) as c:
        c.db = db
        c.box = box
        c.fake = fake
        yield c


def seed(db, rows=None):
    LtxRepo(db).upsert_many(rows or [row(1), row(2)], now_iso())


# ---------------------------------------------------------------- 浏览


def test_dates_empty(app):
    assert app.get("/api/ltx/dates").json() == []


def test_dates_after_sync_layout(app, db):
    seed(db)
    r = app.get("/api/ltx/dates")
    assert r.status_code == 200
    assert r.json() == [{"date": "2026-10-25", "count": 2}]


def test_posts_by_date_contract(app, db):
    """★ 字段名必须与 /api/posts 一致——前端复用同一套卡片渲染。"""
    seed(db)
    r = app.get("/api/ltx/posts", params={"date": "2026-10-25"})
    assert r.status_code == 200
    item = r.json()[0]
    assert set(item) == {
        "tid", "title", "code", "actress", "release_date", "size", "cover",
        "detail", "ed2k", "post_date", "status", "emby_in_library",
        "tg_sent_at", "cover_orig", "detail_orig",
    }
    assert item["code"] == "SSIS-157"
    assert item["size"] == "3.4GB"
    assert item["status"] == "done"
    assert item["emby_in_library"] is False
    # 卡片封面走本地代理端点，浏览器逐张取（列表接口不下载图片）
    assert item["cover"] == f"/api/ltx/cover/{item['tid']}"
    # 灯箱拿 1200px 档
    assert item["cover_orig"] == f"/api/ltx/cover/{item['tid']}?w=1200"


def test_posts_unknown_date_is_empty(app):
    assert app.get("/api/ltx/posts", params={"date": "1999-01-01"}).json() == []


def test_posts_rejects_bad_date(app):
    r = app.get("/api/ltx/posts", params={"date": "2026/10/25"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_date"


def test_posts_requires_date(app):
    assert app.get("/api/ltx/posts").status_code == 422


def test_posts_hides_rejected(app, db):
    seed(db, [row(1), row(2, rejected=1)])
    ids = [p["tid"] for p in app.get(
        "/api/ltx/posts", params={"date": "2026-10-25"}).json()]
    assert ids == [1]


# ---------------------------------------------------------------- 状态 / 同步


def test_status_reports_unconfigured(app):
    body = app.get("/api/ltx/status").json()
    assert body["configured"] is False
    assert body["count"] == 0
    assert body["ttl"] == config.LTX_SYNC_TTL


def test_sync_upserts_and_marks_time(app, db, monkeypatch):
    monkeypatch.setattr(
        ltx_routes.LtxSource, "fetch_all",
        lambda self: [row(1), row(2, date="2026-10-24")],
    )
    r = app.post("/api/ltx/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fetched"] == 2
    assert body["count"] == 2
    assert body["synced_at"]

    # 同步时间落到设置里（status 能读到）
    assert app.get("/api/ltx/status").json()["synced_at"] == body["synced_at"]


def test_sync_prunes_removed_items(app, db, monkeypatch):
    seed(db, [row(1), row(2)])
    monkeypatch.setattr(
        ltx_routes.LtxSource, "fetch_all", lambda self: [row(1)]
    )
    body = app.post("/api/ltx/sync").json()
    assert body["pruned"] == 1
    assert body["count"] == 1


def test_sync_empty_result_does_not_wipe(app, db, monkeypatch):
    """★ 上游临时返回空 → 不能把本地镜像清空。"""
    seed(db, [row(1)])
    monkeypatch.setattr(ltx_routes.LtxSource, "fetch_all", lambda self: [])
    body = app.post("/api/ltx/sync").json()
    assert body["pruned"] == 0
    assert body["count"] == 1


def test_sync_unconfigured_returns_400(app, monkeypatch):
    def boom(self):
        raise AvdbNotConfigured("未配置 LTX 数据源")

    monkeypatch.setattr(ltx_routes.LtxSource, "fetch_all", boom)
    r = app.post("/api/ltx/sync")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ltx_not_configured"


def test_sync_auth_error_returns_502(app, monkeypatch):
    """★ 上游账号问题必须是 502 而不是 401——401 会被前端当成门禁失效。"""

    def boom(self):
        raise AvdbAuthError("账号密码错")

    monkeypatch.setattr(ltx_routes.LtxSource, "fetch_all", boom)
    r = app.post("/api/ltx/sync")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ltx_auth"


def test_sync_upstream_error_returns_502(app, monkeypatch):
    def boom(self):
        raise AvdbError("AVdb 请求失败：timeout")

    monkeypatch.setattr(ltx_routes.LtxSource, "fetch_all", boom)
    r = app.post("/api/ltx/sync")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ltx_upstream"


def test_sync_concurrent_returns_409(app, monkeypatch):
    ltx_routes._sync_lock.acquire()
    try:
        r = app.post("/api/ltx/sync")
    finally:
        ltx_routes._sync_lock.release()
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ltx_syncing"


# ---------------------------------------------------------------- 封面


def test_cover_unknown_item_404(app):
    assert app.get("/api/ltx/cover/999").status_code == 404


def test_cover_without_image_dir_404(app, db):
    seed(db)
    r = app.get("/api/ltx/cover/1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "no_images"


def test_cover_rejects_unknown_width(app, db):
    seed(db)
    r = app.get("/api/ltx/cover/1", params={"w": 999})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_width"


# ---------------------------------------------------------------- 转发


def test_forward_one_ok(app, db):
    seed(db)
    r = app.post("/api/ltx/1/forward")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "message_id": 42}
    assert app.fake.calls == ["ed2k://|file|x.mp4|1|AB|/"]
    assert LtxRepo(db).get(1).tg_sent_at is not None


def test_forward_one_twice_returns_409(app, db):
    seed(db)
    app.post("/api/ltx/1/forward")
    r = app.post("/api/ltx/1/forward")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "tg_already_sent"
    assert len(app.fake.calls) == 1


def test_forward_one_unknown_returns_404(app):
    r = app.post("/api/ltx/999/forward")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_forward_one_without_ed2k_returns_502(app, db):
    seed(db, [row(1, ed2k=None)])
    r = app.post("/api/ltx/1/forward")
    assert r.status_code == 502


def test_forward_day_batch(app, db):
    seed(db, [row(1), row(2)])
    r = app.post("/api/ltx/forward", params={"date": "2026-10-25"})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": 2, "skipped": 0, "failed": 0, "errors": []}


def test_forward_day_skips_already_sent(app, db):
    seed(db, [row(1), row(2)])
    LtxRepo(db).mark_tg_sent(1, "2026-10-26T10:00:00+08:00")
    body = app.post("/api/ltx/forward", params={"date": "2026-10-25"}).json()
    assert body["sent"] == 1
    assert body["skipped"] == 1


def test_forward_day_unknown_date_404(app):
    r = app.post("/api/ltx/forward", params={"date": "1999-01-01"})
    assert r.status_code == 404


def test_forward_day_bad_date_400(app):
    r = app.post("/api/ltx/forward", params={"date": "x"})
    assert r.status_code == 400


def test_forward_day_not_configured_returns_400(app, monkeypatch):
    from bt169.mtpproto import MtpNotConfigured

    def boom(settings):
        raise MtpNotConfigured("未配置 MTProto")

    monkeypatch.setattr(ltx_routes, "get_sender", boom)
    seed(app.db)
    r = app.post("/api/ltx/forward", params={"date": "2026-10-25"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "tg_not_configured"


# ---------------------------------------------------------------- 门禁


def test_ltx_paths_are_gated(db, box):
    """门禁开启后 /api/ltx/* 必须 401（与其它 /api 一致）。"""
    from bt169.api.auth import GATE_KEY
    from bt169.crypto import hash_password
    from bt169.repo.settings import SettingsRepo

    SettingsRepo(db, box).put(GATE_KEY, hash_password("pw"))
    application = create_app(db, box=box, ui_dir=None, image_dir=None)
    with TestClient(application) as c:
        assert c.get("/api/ltx/dates").status_code == 401
