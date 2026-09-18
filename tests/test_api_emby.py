"""Emby 接口（P7 / E-1~E-7）。

全部离线：注入假 HTTP，不打真实 Emby。
"""

from __future__ import annotations

import json

import pytest

from bt169 import config
from bt169.repo.settings import SettingsRepo


class FakeHTTP:
    def __init__(self, *, status=200, body="{}", raise_exc=None):
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}, headers or {}))
        if self.raise_exc is not None:
            raise self.raise_exc

        class R:
            pass

        r = R()
        r.status_code = self.status
        r.text = self.body
        return r

    def close(self):
        pass


def items(*specs):
    return json.dumps({"Items": [{"Id": i, "Name": n} for i, n in specs],
                       "TotalRecordCount": len(specs)})


def seed_post(db, tid=101, code="START-624"):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,code,actress,release_date,size,"
            " cover_img,detail_img,ed2k,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"标题 {tid}", code, "某", "2026-09-17", "7GB",
             None, None, "ed2k://|file|a|1|A|/", "2026-09-14", "done",
             now_iso(), now_iso()),
        )


def set_emby(db, box, *, url="http://emby.local:8096", key="K", library=None):
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("emby", "url"), url)
    sr.put(config.section_key("emby", "api_key"), key)
    if library:
        sr.put(config.section_key("emby", "library"), library)


@pytest.fixture()
def emby_app(db, box, monkeypatch):
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.api.routes import emby as emby_routes
    from bt169.emby import EmbyClient

    http = FakeHTTP(body=items(("e1", "START-624")))
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: EmbyClient(url="http://emby.local:8096", api_key="K",
                                    http=http),
    )

    app = create_app(db, box=box, ui_dir=None)
    with TestClient(app) as c:
        c.http = http        # type: ignore[attr-defined]
        c.db = db            # type: ignore[attr-defined]
        c.box = box          # type: ignore[attr-defined]
        yield c


# ---------------------------------------------------------------- 刷新


def test_refresh_ok(emby_app):
    seed_post(emby_app.db, 101, "START-624")
    set_emby(emby_app.db, emby_app.box)

    r = emby_app.post("/api/emby/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["checked"] == 1 and body["in_library"] == 1
    assert body["error"] is None


def test_refresh_marks_posts(emby_app):
    seed_post(emby_app.db, 101, "START-624")
    seed_post(emby_app.db, 102, "MIDA-737")
    set_emby(emby_app.db, emby_app.box)
    emby_app.post("/api/emby/refresh")

    from bt169.repo.posts import PostRepo
    pr = PostRepo(emby_app.db)
    assert pr.get(101).emby_status == "in_library"
    assert pr.get(102).emby_status == "none"


def test_refresh_without_config_is_ok_false_not_400(emby_app, monkeypatch):
    """★ 未配置时返回 200 + ok:false。

    设置面板的「刷新」按钮要**显示**原因；用 400 会让前端走通用错误分支，
    用户只看到「请求失败」而不知道是没配地址。
    """
    from bt169.api.routes import emby as emby_routes
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: (_ for _ in ()).throw(
            emby_routes.EmbyNotConfigured("尚未配置 Emby 地址或 API Key")),
    )
    r = emby_app.post("/api/emby/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "尚未配置" in body["error"]


def test_refresh_emby_down_is_ok_false(emby_app, monkeypatch):
    """★ E-7：Emby 不可达时**不返回 5xx**，且**不抛异常**。"""
    from bt169.api.routes import emby as emby_routes
    from bt169.emby import EmbyClient

    bad = FakeHTTP(raise_exc=OSError("连接被拒"))
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: EmbyClient(url="http://e", api_key="K", http=bad),
    )
    seed_post(emby_app.db, 101)
    set_emby(emby_app.db, emby_app.box)

    r = emby_app.post("/api/emby/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "连接被拒" in body["error"]


def test_refresh_down_does_not_clear_existing_marks(emby_app, monkeypatch):
    """★ Emby 挂了不该把已有的「已入库」标记抹掉。"""
    from bt169.api.routes import emby as emby_routes
    from bt169.emby import EmbyClient
    from bt169.repo.posts import PostRepo

    seed_post(emby_app.db, 101, "START-624")
    set_emby(emby_app.db, emby_app.box)
    emby_app.post("/api/emby/refresh")
    assert PostRepo(emby_app.db).get(101).emby_status == "in_library"

    bad = FakeHTTP(raise_exc=OSError("挂了"))
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: EmbyClient(url="http://e", api_key="K", http=bad),
    )
    r = emby_app.post("/api/emby/refresh")
    assert r.json()["ok"] is False
    assert PostRepo(emby_app.db).get(101).emby_status == "in_library"


def test_refresh_uses_configured_library(emby_app, monkeypatch):
    """E-4：只查设置页指定的媒体库。"""
    from bt169.api.routes import emby as emby_routes
    from bt169.emby import EmbyClient

    seen = {}

    def build(settings):
        seen["library"] = settings.get(config.section_key("emby", "library"))
        return EmbyClient(url="http://e", api_key="K", http=emby_app.http,
                          library_id=seen["library"])

    monkeypatch.setattr(emby_routes, "build_client", build)
    set_emby(emby_app.db, emby_app.box, library="lib42")
    emby_app.post("/api/emby/refresh")

    _, params, _ = emby_app.http.calls[0]
    assert params["ParentId"] == "lib42"


def test_refresh_is_idempotent(emby_app):
    """重复刷新结果一致，不会把状态翻转。"""
    from bt169.repo.posts import PostRepo
    seed_post(emby_app.db, 101, "START-624")
    set_emby(emby_app.db, emby_app.box)

    emby_app.post("/api/emby/refresh")
    emby_app.post("/api/emby/refresh")
    pr = PostRepo(emby_app.db)
    assert pr.get(101).emby_status == "in_library"
    assert pr.get(101).emby_item_id == "e1"


# ---------------------------------------------------------------- 库列表


def test_libraries_ok(emby_app, monkeypatch):
    from bt169.api.routes import emby as emby_routes

    body = json.dumps([{"Name": "电影", "ItemId": "lib1"},
                       {"Name": "番剧", "ItemId": "lib2"}])
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K", http=FakeHTTP(body=body)),
    )
    r = emby_app.get("/api/emby/libraries")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["libraries"] == [{"id": "lib1", "name": "电影"},
                                 {"id": "lib2", "name": "番剧"}]


def test_libraries_without_config(emby_app, monkeypatch):
    from bt169.api.routes import emby as emby_routes
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: (_ for _ in ()).throw(
            emby_routes.EmbyNotConfigured("尚未配置 Emby 地址或 API Key")),
    )
    r = emby_app.get("/api/emby/libraries")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["libraries"] == []


def test_libraries_down_returns_empty(emby_app, monkeypatch):
    from bt169.api.routes import emby as emby_routes
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K",
            http=FakeHTTP(raise_exc=OSError("连不上"))),
    )
    r = emby_app.get("/api/emby/libraries")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "连不上" in body["error"]


# ---------------------------------------------------------------- DTO 契约


def test_posts_api_exposes_emby_flag(emby_app):
    """E-2：卡片标记靠 ``/api/posts`` 的 ``emby_in_library``。"""
    seed_post(emby_app.db, 101, "START-624")
    set_emby(emby_app.db, emby_app.box)
    emby_app.post("/api/emby/refresh")

    r = emby_app.get("/api/posts?date=2026-09-14")
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert row["emby_in_library"] is True


def test_posts_api_emby_false_when_absent(emby_app):
    seed_post(emby_app.db, 101, "START-624")
    set_emby(emby_app.db, emby_app.box)
    emby_app.post("/api/emby/refresh")

    r = emby_app.get("/api/posts?date=2026-09-14")
    assert r.json()[0]["emby_in_library"] is True

    seed_post(emby_app.db, 103, "MIDA-737")
    r = emby_app.get("/api/posts?date=2026-09-14")
    rows = {row["tid"]: row for row in r.json()}
    assert rows[103]["emby_in_library"] is False


def test_refresh_never_leaks_api_key(emby_app):
    """★ API Key 绝不能出现在任何响应里。"""
    seed_post(emby_app.db, 101)
    set_emby(emby_app.db, emby_app.box, key="SUPER-SECRET-KEY")
    r = emby_app.post("/api/emby/refresh")
    assert "SUPER-SECRET-KEY" not in r.text


# ---------------------------------------------------------------- 定时器装配


def test_app_does_not_start_scheduler_by_default(db, box):
    """★ 测试入口不能默认开后台线程。

    每个测试都建 app；默认开启会给每个测试起一个线程、且一启动就跑同步。
    """
    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None)
    assert not hasattr(app.state, "emby_scheduler")


def test_app_starts_scheduler_when_enabled(db, box):
    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None, emby_interval=9999)
    assert app.state.emby_scheduler.interval == 9999


def test_app_scheduler_negative_interval_uses_default(db, box):
    """``-1`` 是「用默认间隔」的哨兵（生产入口用它）。"""
    from bt169.api.app import create_app
    from bt169.emby import SYNC_INTERVAL_SECONDS

    app = create_app(db, box=box, ui_dir=None, emby_interval=-1)
    assert app.state.emby_scheduler.interval == SYNC_INTERVAL_SECONDS


def test_app_scheduler_zero_disables(db, box):
    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None, emby_interval=0)
    assert not hasattr(app.state, "emby_scheduler")
