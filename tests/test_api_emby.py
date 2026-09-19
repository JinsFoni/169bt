"""Emby 接口（P7 / E-1~E-7）。

全部离线：注入假 HTTP，不打真实 Emby。
"""

from __future__ import annotations

import json
import time

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


# ---------------------------------------------------- 按用户过滤媒体库（S-8）


def _route_http(*, users=None, views=None, vf=None, raise_exc=None):
    """按路径分发的假 HTTP。

    ``/Users``           → 顶层数组
    ``/Users/{id}/Views`` → {"Items": [...]}
    ``/Library/VirtualFolders`` → 顶层数组
    """
    class H:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params=None, headers=None, timeout=None):
            self.calls.append(url)
            if raise_exc is not None:
                raise raise_exc
            if url.endswith("/Users"):
                body = json.dumps(users or [])
            elif "/Views" in url:
                body = json.dumps({"Items": views or []})
            else:
                body = json.dumps(vf or [])

            class R:
                pass

            r = R()
            r.status_code = 200
            r.text = body
            return r

        def close(self):
            pass

    return H()


def _set_user(db, box, name):
    SettingsRepo(db, box).put(config.section_key("emby", "username"), name)


def test_libraries_filters_by_username(emby_app, monkeypatch):
    """★ 配了用户名 → 只返回该用户可见的库（用户新需求）。"""
    from bt169.api.routes import emby as emby_routes

    set_emby(emby_app.db, emby_app.box)
    _set_user(emby_app.db, emby_app.box, "muse")
    http = _route_http(
        users=[{"Name": "muse", "Id": "U1"}],
        views=[{"Id": "1373", "Name": "有码"}, {"Id": "332", "Name": "4K"}],
        vf=[{"ItemId": "999", "Name": "不该出现"}],
    )
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K", http=http),
    )
    r = emby_app.get("/api/emby/libraries")
    body = r.json()
    assert body["ok"] is True
    assert body["libraries"] == [{"id": "1373", "name": "有码"},
                                 {"id": "332", "name": "4K"}]
    assert body["source"] == "user" and body["filtered_by"] == "muse"
    # ★ 走的是 /Users/{id}/Views，不是 VirtualFolders
    assert any("/Users/U1/Views" in u for u in http.calls)
    assert not any("VirtualFolders" in u for u in http.calls)


def test_libraries_without_username_uses_admin_view(emby_app, monkeypatch):
    """★ 没配用户名 → 退回管理员视角（全部库），并说明未过滤。"""
    from bt169.api.routes import emby as emby_routes

    set_emby(emby_app.db, emby_app.box)
    http = _route_http(vf=[{"ItemId": "lib1", "Name": "电影"}])
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K", http=http),
    )
    r = emby_app.get("/api/emby/libraries")
    body = r.json()
    assert body["ok"] is True
    assert body["libraries"] == [{"id": "lib1", "name": "电影"}]
    assert body["source"] == "all" and body["filtered_by"] is None
    assert any("VirtualFolders" in u for u in http.calls)


def test_libraries_unknown_username_falls_back(emby_app, monkeypatch):
    """★ 用户名在 Emby 里不存在 → 退回全部库 + 提示（方案 A）。

    过滤只是「帮你少看几个库」，不是安全边界——找不到人时宁可多给
    几个库，也不要让设置页整个用不了。
    """
    from bt169.api.routes import emby as emby_routes

    set_emby(emby_app.db, emby_app.box)
    _set_user(emby_app.db, emby_app.box, "查无此人")
    http = _route_http(
        users=[{"Name": "muse", "Id": "U1"}],
        vf=[{"ItemId": "lib1", "Name": "电影"}],
    )
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K", http=http),
    )
    r = emby_app.get("/api/emby/libraries")
    body = r.json()
    assert body["ok"] is True
    assert body["libraries"] == [{"id": "lib1", "name": "电影"}]
    assert body["source"] == "all"
    assert "查无此人" in (body["warning"] or "")


def test_libraries_case_mismatch_falls_back(emby_app, monkeypatch):
    """★ 大小写不匹配 = 找不到 → 退回全部库（不静默匹配别人）。"""
    from bt169.api.routes import emby as emby_routes

    set_emby(emby_app.db, emby_app.box)
    _set_user(emby_app.db, emby_app.box, "Muse")   # 库里只有 muse
    http = _route_http(
        users=[{"Name": "muse", "Id": "U1"}],
        vf=[{"ItemId": "lib1", "Name": "电影"}],
    )
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: emby_routes.EmbyClient(
            url="http://e", api_key="K", http=http),
    )
    r = emby_app.get("/api/emby/libraries")
    body = r.json()
    assert body["source"] == "all"
    assert body["libraries"] == [{"id": "lib1", "name": "电影"}]
    assert "Muse" in (body["warning"] or "")


# ------------------------------------------------- 采集完成后立即检查


def test_collect_done_triggers_emby_check(db, box, monkeypatch):
    """★ 采集任务成功收尾后，立即对本次采到的帖子做一次 Emby 检查。

    端到端走**真实装配路径**（route → ``build_runner`` 装钩子 →
    采集收尾回调 → Emby）：新帖的入库标记当场出现，不等 15 分钟定时器。
    """
    from bt169.api.app import create_app
    from bt169.api.routes import collect as collect_routes
    from bt169.api.routes import emby as emby_routes
    from bt169.collector import Collector
    from bt169.emby import EmbyClient
    from bt169.repo.posts import PostRepo
    from bt169.source.parse import ListRow, ThreadDetail
    from fastapi.testclient import TestClient

    # Emby 假 HTTP：媒体库里只有 START-11
    http = FakeHTTP(body=items(("e1", "START-11")))
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: EmbyClient(url="http://e", api_key="K", http=http),
    )

    # 论坛假客户端：两帖,番号分别 START-11（应命中）与 START-22
    class FC:
        def list_page(self, *, fid="192", page=1):
            return [ListRow(tid=11, title="T11", post_date="2026-09-14",
                            reply_count=0),
                    ListRow(tid=22, title="T22", post_date="2026-09-14",
                            reply_count=0)]

        def fetch_thread(self, tid):
            return ThreadDetail(
                tid=tid, title=f"标题 {tid}", code=f"START-{tid}", actress="某",
                release_date="2026-09-17", size="7GB",
                cover_img=None, detail_img=None,
                ed2k=f"ed2k://|file|{tid}.mkv|1|AB|/", locked=False,
            )

        def close(self):
            pass

    # ★ 不自建 runner：monkeypatch build_collector，让 build_runner 走
    #   真实路径装钩子——自建 runner 等于绕过了被测的接线本身。
    monkeypatch.setattr(
        collect_routes, "build_collector",
        lambda app: Collector(client=FC(), posts=PostRepo(db),
                              jobs=collect_routes.CollectRepo(db)),
    )

    app = create_app(db, box=box, ui_dir=None)

    with TestClient(app) as c:
        r = c.post("/api/collect",
                   json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job"]["id"]

        # 后台线程同步等它跑完（采集无网络,立即完成）
        deadline = time.time() + 10
        while time.time() < deadline:
            s = c.get("/api/collect/status").json()
            jobs = [j for j in s.get("recent", []) if j["id"] == job_id]
            if jobs and jobs[0]["status"] == "done":
                break
            time.sleep(0.05)
        assert jobs and jobs[0]["status"] == "done", "采集应已成功收尾"

    posts = PostRepo(db)
    p11, p22 = posts.get(11), posts.get(22)
    # ★ 立即检查已发生：命中库的帖子带标记 + Emby item id
    assert p11.emby_status == "in_library" and p11.emby_item_id == "e1"
    # 未命中的也要写「查过但不在库」（否则前端拿旧缓存）
    assert p22.emby_status == "none" and p22.emby_item_id is None
    assert p11.emby_checked and p22.emby_checked
    assert http.calls, "采集收尾必须发起过一次 Emby 检查"


def test_collect_done_callback_failure_does_not_fail_job(db, box, monkeypatch):
    """Emby 挂了（同步器返回 error）→ 任务照常 done,不受牵连（E-7）。"""
    from bt169.api.app import create_app
    from bt169.api.routes import collect as collect_routes
    from bt169.api.routes import emby as emby_routes
    from bt169.collector import Collector
    from bt169.emby import EmbyClient
    from bt169.repo.posts import PostRepo
    from bt169.source.parse import ListRow, ThreadDetail
    from fastapi.testclient import TestClient

    http = FakeHTTP(raise_exc=OSError("Emby 连接被拒"))
    monkeypatch.setattr(
        emby_routes, "build_client",
        lambda settings: EmbyClient(url="http://e", api_key="K", http=http),
    )

    class FC:
        def list_page(self, *, fid="192", page=1):
            return [ListRow(tid=11, title="T11", post_date="2026-09-14",
                            reply_count=0)]

        def fetch_thread(self, tid):
            return ThreadDetail(
                tid=tid, title=f"标题 {tid}", code=f"START-{tid}", actress="某",
                release_date="2026-09-17", size="7GB",
                cover_img=None, detail_img=None,
                ed2k=f"ed2k://|file|{tid}.mkv|1|AB|/", locked=False,
            )

        def close(self):
            pass

    monkeypatch.setattr(
        collect_routes, "build_collector",
        lambda app: Collector(client=FC(), posts=PostRepo(db),
                              jobs=collect_routes.CollectRepo(db)),
    )
    app = create_app(db, box=box, ui_dir=None)

    with TestClient(app) as c:
        r = c.post("/api/collect",
                   json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
        job_id = r.json()["job"]["id"]
        deadline = time.time() + 10
        jobs = []
        while time.time() < deadline:
            s = c.get("/api/collect/status").json()
            jobs = [j for j in s.get("recent", []) if j["id"] == job_id]
            if jobs and jobs[0]["status"] == "done":
                break
            time.sleep(0.05)
        assert jobs and jobs[0]["status"] == "done", "Emby 失败不能拖垮采集"
        p = PostRepo(db).get(11)
        assert p.emby_status is None, "Emby 不可达时不应写入任何状态"


# ---------------------------------------------------------------- cron 定时刷新


class TestEmbyDelaySeconds:
    """``emby_delay_seconds``：cron 驱动的下次触发间隔（镜像 RSS 模式）。"""

    def test_empty_returns_default_interval(self):
        """★ 空 cron = 保持现状：默认每 15 分钟一次（E-6 现行为不变）。"""
        from bt169.api.app import EMBY_FALLBACK_SECONDS, emby_delay_seconds

        assert emby_delay_seconds("") == EMBY_FALLBACK_SECONDS
        assert emby_delay_seconds("   ") == EMBY_FALLBACK_SECONDS
        assert emby_delay_seconds(None) == EMBY_FALLBACK_SECONDS

    def test_invalid_falls_back(self):
        """理论上存不进库（保存时已 400），但旧库/手改库不能弄死线程。"""
        from bt169.api.app import EMBY_FALLBACK_SECONDS, emby_delay_seconds

        assert emby_delay_seconds("nonsense") == EMBY_FALLBACK_SECONDS
        assert emby_delay_seconds("60 * * * *") == EMBY_FALLBACK_SECONDS

    def test_valid_returns_seconds_until_next(self):
        from bt169.api.app import emby_delay_seconds

        delay = emby_delay_seconds("*/30 * * * *")
        assert 1.0 <= delay <= 30 * 60

    def test_clamped_to_at_least_one_second(self):
        """NTP 回拨防线：结果恒 ≥ 1 秒，防忙循环。"""
        from bt169.api.app import emby_delay_seconds

        assert emby_delay_seconds("* * * * *", wall=lambda: 0.0) >= 1.0


class TestEmbySchedulerCron:
    """装配：启用定时器时必须挂上 cron 闭包（每次触发重读 emby.cron）。"""

    def test_scheduler_has_next_delay_when_enabled(self, db, box):
        from bt169.api.app import create_app

        app = create_app(db, box=box, ui_dir=None, emby_interval=9999)
        sched = app.state.emby_scheduler
        assert sched.next_delay is not None
        # 空 cron → 默认间隔；填了 cron → 按 cron 算
        assert sched.next_delay() == 15 * 60
        SettingsRepo(db, box).put("emby.cron", "*/30 * * * *")
        assert 1.0 <= sched.next_delay() <= 30 * 60

    def test_scheduler_without_cron_field_still_interval_mode(self, db, box):
        """未启用定时器（interval=0）时什么都不装——现状不变。"""
        from bt169.api.app import create_app

        app = create_app(db, box=box, ui_dir=None, emby_interval=0)
        assert not hasattr(app.state, "emby_scheduler")
