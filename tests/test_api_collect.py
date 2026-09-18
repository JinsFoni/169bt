"""采集接口测试。用假论坛客户端，不碰网络。"""

from __future__ import annotations

import pytest

from bt169.collector import CollectRunner, Collector
from bt169.repo.collect import JOB_DONE, CollectRepo
from bt169.repo.posts import PostRepo
from bt169.source.parse import ListRow, ThreadDetail


class FakeForum:
    def __init__(self, pages=None, details=None):
        self.pages = pages or {}
        self.details = details or {}
        self.fetch_calls: list[int] = []

    def list_page(self, *, fid="192", page=1):
        return self.pages.get(page, [])

    def fetch_thread(self, tid):
        self.fetch_calls.append(tid)
        return self.details[tid]

    def close(self):
        pass


def rows(*specs):
    return [ListRow(tid=t, title=f"T{t}", post_date=d, reply_count=0)
            for t, d in specs]


def detail(tid, ed2k=True):
    return ThreadDetail(
        tid=tid, title=f"标题 {tid}", code=f"ABC-{tid}", actress="某",
        release_date="2026-09-17", size="7GB", cover_img=None, detail_img=None,
        ed2k=f"ed2k://|file|{tid}.mkv|1|AB|/" if ed2k else None, locked=not ed2k,
    )


@pytest.fixture()
def fake_app(db, box):
    """构造 app 并把 collect_runner 换成假客户端驱动的 runner。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None)
    fc = FakeForum()
    runner = CollectRunner(
        Collector(client=fc, posts=PostRepo(db), jobs=CollectRepo(db)),
        CollectRepo(db),
    )
    app.state.collect_runner = runner
    with TestClient(app) as c:
        c.fake_forum = fc          # type: ignore[attr-defined]
        c.runner = runner          # type: ignore[attr-defined]
        yield c


# ------------------------------------------------------------ 启动采集


def test_start_collect_returns_202(fake_app):
    fake_app.fake_forum.pages = {1: rows((101, "2026-09-14"))}
    fake_app.fake_forum.details = {101: detail(101)}

    r = fake_app.post("/api/collect",
                      json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    assert r.status_code == 202
    body = r.json()
    assert body["job"]["id"] > 0
    assert body["job"]["from_date"] == "2026-09-14"
    assert body["job"]["fid"] == "192"
    fake_app.runner.join(timeout=5)


def test_start_collect_invalid_range_returns_400(fake_app):
    r = fake_app.post("/api/collect",
                      json={"from_date": "2026-09-14", "to_date": "2026-09-01"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_range"
    assert "不能晚于" in r.json()["error"]["message"]


def test_start_collect_bad_format_returns_400(fake_app):
    r = fake_app.post("/api/collect",
                      json={"from_date": "2026/09/14", "to_date": "2026-09-14"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_range"


def test_start_collect_span_limit_returns_400(fake_app):
    r = fake_app.post("/api/collect",
                      json={"from_date": "2020-01-01", "to_date": "2026-09-14"})
    assert r.status_code == 400
    assert "跨度" in r.json()["error"]["message"]


def test_second_collect_returns_409(fake_app):
    """★ 并发度恒为 1。"""
    fake_app.fake_forum.pages = {
        1: rows(*[(100 + i, "2026-09-14") for i in range(40)])
    }
    fake_app.fake_forum.details = {100 + i: detail(100 + i) for i in range(40)}

    r1 = fake_app.post("/api/collect",
                       json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    assert r1.status_code == 202
    try:
        r2 = fake_app.post("/api/collect",
                           json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "collect_running"
    finally:
        fake_app.runner.join(timeout=10)


def test_missing_fields_returns_422(fake_app):
    r = fake_app.post("/api/collect", json={})
    assert r.status_code == 422


# ------------------------------------------------------------ 状态查询


def test_status_empty_initially(fake_app):
    r = fake_app.get("/api/collect/status")
    assert r.status_code == 200
    body = r.json()
    assert body["job"] is None
    assert body["recent"] == []


def test_status_reports_active_job(fake_app):
    fake_app.fake_forum.pages = {
        1: rows(*[(100 + i, "2026-09-14") for i in range(40)])
    }
    fake_app.fake_forum.details = {100 + i: detail(100 + i) for i in range(40)}
    fake_app.post("/api/collect",
                  json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    try:
        r = fake_app.get("/api/collect/status")
        body = r.json()
        assert body["job"] is not None
        assert body["job"]["status"] == "running"
        assert "percent" in body["job"]
    finally:
        fake_app.runner.join(timeout=10)


def test_status_after_completion(fake_app):
    fake_app.fake_forum.pages = {1: rows((101, "2026-09-14"))}
    fake_app.fake_forum.details = {101: detail(101)}
    fake_app.post("/api/collect",
                  json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    fake_app.runner.join(timeout=5)

    r = fake_app.get("/api/collect/status")
    body = r.json()
    assert body["job"] is None                 # 已无运行中任务
    assert body["recent"][0]["status"] == JOB_DONE
    assert body["recent"][0]["collected"] == 1


def test_job_detail_by_id(fake_app):
    fake_app.fake_forum.pages = {1: rows((101, "2026-09-14"))}
    fake_app.fake_forum.details = {101: detail(101)}
    job_id = fake_app.post(
        "/api/collect",
        json={"from_date": "2026-09-14", "to_date": "2026-09-14"},
    ).json()["job"]["id"]
    fake_app.runner.join(timeout=5)

    r = fake_app.get(f"/api/collect/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["job"]["id"] == job_id


def test_job_detail_404(fake_app):
    r = fake_app.get("/api/collect/jobs/99999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------ 取消


def test_cancel_unknown_job_404(fake_app):
    r = fake_app.post("/api/collect/jobs/99999/cancel")
    assert r.status_code == 404


def test_cancel_finished_job_409(fake_app):
    fake_app.fake_forum.pages = {1: rows((101, "2026-09-14"))}
    fake_app.fake_forum.details = {101: detail(101)}
    job_id = fake_app.post(
        "/api/collect",
        json={"from_date": "2026-09-14", "to_date": "2026-09-14"},
    ).json()["job"]["id"]
    fake_app.runner.join(timeout=5)

    r = fake_app.post(f"/api/collect/jobs/{job_id}/cancel")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_running"


def test_cancel_running_job(fake_app):
    fake_app.fake_forum.pages = {
        1: rows(*[(100 + i, "2026-09-14") for i in range(40)])
    }
    fake_app.fake_forum.details = {100 + i: detail(100 + i) for i in range(40)}
    job_id = fake_app.post(
        "/api/collect",
        json={"from_date": "2026-09-14", "to_date": "2026-09-14"},
    ).json()["job"]["id"]
    try:
        r = fake_app.post(f"/api/collect/jobs/{job_id}/cancel")
        assert r.status_code == 200
        assert r.json()["job"]["cancel_requested"] is True
    finally:
        fake_app.runner.join(timeout=10)


# ------------------------------------------------------------ 采集结果落库


def test_collected_posts_browsable(fake_app):
    """★ 采集结果要能立刻在浏览接口看到（端到端）。"""
    fake_app.fake_forum.pages = {1: rows((101, "2026-09-14"))}
    fake_app.fake_forum.details = {101: detail(101)}
    fake_app.post("/api/collect",
                  json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    fake_app.runner.join(timeout=5)

    r = fake_app.get("/api/posts", params={"date": "2026-09-14"})
    assert r.status_code == 200
    posts = r.json()               # 契约：裸数组（与前端 app.js 一致）
    assert isinstance(posts, list)
    assert len(posts) == 1
    assert posts[0]["tid"] == 101
    assert posts[0]["code"] == "ABC-101"
    assert posts[0]["ed2k"].startswith("ed2k://")


# ------------------------------------------------------------ /api/status


def test_status_endpoint_shape(client):
    """★ 契约与 FRONTEND.md §14.1 对齐（避免前后端字段漂移）。"""
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "version", "session", "collector", "collect", "library",
        "telegram",            # T-4：前端据此禁用「下载」按钮
        "last_error",
    }
    # ★ 只回布尔值——绝不能把 token/chat_id 漏进这个未脱敏的接口
    assert body["telegram"] == {"configured": False}
    assert body["session"]["valid"] is False
    assert body["session"]["relogin_state"] == "ok"
    assert body["collect"]["running"] is False
    assert body["library"]["total"] == 0
    assert body["collector"]["consecutive_failures"] == 0


def test_status_never_leaks_cookies(db, box, client):
    """★ 安全：状态接口绝不能带出 Cookie 内容。"""
    from bt169.source.session import SessionStore

    SessionStore(db).save_cookies(
        {"SlDj_2132_auth": "SUPER_SECRET_TOKEN"}, username="u"
    )
    body = client.get("/api/status").text
    assert "SUPER_SECRET_TOKEN" not in body
    assert "SlDj_2132_auth" not in body


def test_status_reports_session_days_left(db, box, client):
    from bt169.source.session import SessionStore

    SessionStore(db).save_cookies({"a": "1"}, username="tester")
    body = client.get("/api/status").json()
    assert body["session"]["valid"] is True
    assert body["session"]["username"] == "tester"
    assert body["session"]["days_left"] >= 28


def test_status_counts_library(client, seed):
    body = client.get("/api/status").json()
    assert body["library"]["total"] == 1
    assert body["library"]["by_status"] == {"done": 1}


def test_status_reports_running_collect(fake_app):
    fake_app.fake_forum.pages = {
        1: rows(*[(100 + i, "2026-09-14") for i in range(40)])
    }
    fake_app.fake_forum.details = {100 + i: detail(100 + i) for i in range(40)}
    fake_app.post("/api/collect",
                  json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    try:
        body = fake_app.get("/api/status").json()
        assert body["collect"]["running"] is True
        assert body["collect"]["job_id"] is not None
    finally:
        fake_app.runner.join(timeout=10)


# ------------------------------------------------------------ 真实 runner 装配
#
# ★ 这一组是回归测试，针对一类很隐蔽的失败：
#
#   其余测试全都把 `app.state.collect_runner` 换成了假 runner，
#   于是 `_build_runner()` 的**每一行都没被执行过**。
#   结果 `SettingsRepo` 忘了 import，336 个测试全绿，
#   而真实请求一进来就是 500 `name 'SettingsRepo' is not defined`。
#
#   教训：替身注入得越彻底，真实装配路径越容易裸奔。
#   必须有一条测试真的去构造 runner。


def _fake_request(db, box, image_dir=None):
    """造一个够 `_build_runner` 用的最小 Request 替身。"""
    from types import SimpleNamespace

    state = SimpleNamespace(db=db, box=box, image_dir=image_dir)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_build_runner_works_without_session(db, box, tmp_path, monkeypatch):
    """★ 无会话时也要能装配出 runner（匿名采集路径）。"""
    from bt169.api.routes import collect as collect_route

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    req = _fake_request(db, box, tmp_path / "images")

    runner = collect_route._build_runner(req)
    assert isinstance(runner, CollectRunner)
    # 挂在 app.state 上，供后续请求复用
    assert req.app.state.collect_runner is runner


def test_build_runner_injects_images(db, box, tmp_path, monkeypatch):
    """★ 有 image_dir 时必须注入 ImageCache（否则图片永远不本地化）。"""
    from bt169.api.routes import collect as collect_route
    from bt169.collector.imagecache import ImageCache

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    req = _fake_request(db, box, tmp_path / "images")

    runner = collect_route._build_runner(req)
    assert isinstance(runner._collector._images, ImageCache)  # type: ignore[attr-defined]


def test_build_runner_skips_images_without_dir(db, box, monkeypatch):
    """image_dir 为 None（API 单测）时不注入，图片失败不该拖垮采集。"""
    from bt169.api.routes import collect as collect_route

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    runner = collect_route._build_runner(_fake_request(db, box, image_dir=None))
    assert runner._collector._images is None  # type: ignore[attr-defined]


def test_build_runner_no_thanks_without_session(db, box, tmp_path, monkeypatch):
    """★ 匿名会话下不注入 ThanksClient：感谢必然失败，白跑一轮限速请求。"""
    from bt169.api.routes import collect as collect_route

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    runner = collect_route._build_runner(_fake_request(db, box, tmp_path / "i"))
    assert runner._collector._thanks is None  # type: ignore[attr-defined]


def test_build_runner_injects_thanks_with_valid_session(db, box, tmp_path, monkeypatch):
    """★ 有有效会话时注入 ThanksClient —— 这才是 C-4 生效的路径。"""
    from bt169.api.routes import collect as collect_route
    from bt169.source.session import SessionStore
    from bt169.source.thanks import ThanksClient

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    store = SessionStore(db)
    store.save_cookies({"cdb_sid": "abc"}, username="tester")

    runner = collect_route._build_runner(_fake_request(db, box, tmp_path / "i"))
    assert isinstance(runner._collector._thanks, ThanksClient)  # type: ignore[attr-defined]


def test_real_collect_endpoint_does_not_500(db, box, tmp_path, monkeypatch):
    """★ 端到端：**不注入**假 runner，走真实 ``_build_runner``。

    这条测试的价值就在于「什么都不替换」——只有真实装配路径被跑到，
    才能发现 import 缺失这类错误。
    """
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.api.routes import collect as collect_route

    monkeypatch.setattr(collect_route, "ForumClient", lambda **kw: FakeForum())
    app = create_app(db, box=box, ui_dir=None, image_dir=tmp_path / "images")
    with TestClient(app) as c:
        r = c.post("/api/collect",
                   json={"from_date": "2026-09-14", "to_date": "2026-09-14"})
    assert r.status_code == 202, r.text


def test_status_telegram_configured_when_both_set(client, db, box):
    """T-4：Token + Chat ID 都填了才算配置完成。"""
    from bt169 import config
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "123:ABC")
    sr.put(config.section_key("tg", "chat_id"), "-100200")

    body = client.get("/api/status").json()
    assert body["telegram"]["configured"] is True


def test_status_telegram_needs_both_fields(client, db, box):
    """★ 只填 Token 没填 Chat ID → 仍未配置。

    发消息需要 chat_id，只配一半时按钮必须保持禁用——否则用户点了
    只会拿到一个 400，比一开始就禁用更困惑。
    """
    from bt169 import config
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "123:ABC")

    body = client.get("/api/status").json()
    assert body["telegram"]["configured"] is False


def test_status_telegram_whitespace_token_is_not_configured(client, db, box):
    """★ 全空白的值不算配置——否则按钮启用、点击必失败。"""
    from bt169 import config
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "   ")
    sr.put(config.section_key("tg", "chat_id"), "  ")

    body = client.get("/api/status").json()
    assert body["telegram"]["configured"] is False


def test_status_never_leaks_telegram_credentials(client, db, box):
    """★ /api/status **不经过**设置面板的脱敏逻辑，必须自己守住。

    这是最容易漏的地方：给前端加个字段顺手把 token 也塞进去，
    而 /api/status 是免认证的……那就是把凭据公开了。
    """
    from bt169 import config
    from bt169.repo.settings import SettingsRepo

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "999999:SECRET-TOKEN-XYZ")
    sr.put(config.section_key("tg", "chat_id"), "-100200")

    raw = client.get("/api/status").text
    assert "SECRET-TOKEN-XYZ" not in raw, "token 泄漏到 /api/status！"
    assert "999999" not in raw, "token 片段泄漏！"
    assert "-100200" not in raw, "chat_id 泄漏！"
