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
        "version", "session", "collector", "collect", "library", "last_error"
    }
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
