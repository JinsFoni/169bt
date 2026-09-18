# tests/test_api.py
def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_needs_no_auth(client):
    """健康检查供 Lucky/存活探测使用，不能要求认证。"""
    assert client.get("/api/health").status_code == 200

# tests/test_api.py  （追加到文件末尾）
import pytest


def test_dates_empty(client):
    r = client.get("/api/dates")
    assert r.status_code == 200
    assert r.json() == []


def test_dates_lists_ascending(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (111, "older", "2026-09-10", "done", now_iso(), now_iso()),
        )
    assert client.get("/api/dates").json() == [
        {"date": "2026-09-10", "count": 1},
        {"date": "2026-09-14", "count": 1},
    ]


def test_dates_excludes_non_browsable(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (222, "pending", "2026-09-15", "pending", now_iso(), now_iso()),
        )
    dates = [d["date"] for d in client.get("/api/dates").json()]
    assert "2026-09-15" not in dates


def test_posts_by_date(client, seed):
    r = client.get("/api/posts", params={"date": "2026-09-14"})
    assert r.status_code == 200
    posts = r.json()
    assert len(posts) == 1
    assert posts[0]["tid"] == 3986000
    assert posts[0]["code"] == "START-624"


def test_posts_contract_fields(client, seed):
    """ADR-11/16：字段名必须是 cover/detail，且无内部字段。"""
    p = client.get("/api/posts", params={"date": "2026-09-14"}).json()[0]
    assert p["cover"] == "https://img/c.jpg"
    assert p["detail"] == "https://img/d.jpg"
    assert set(p) == {
        "tid", "title", "code", "actress", "release_date", "size",
        "cover", "detail", "ed2k", "post_date", "status", "emby_in_library",
        "tg_sent_at",          # T-7：已转发状态
    }


def test_posts_unknown_date_is_empty(client, seed):
    r = client.get("/api/posts", params={"date": "1999-01-01"})
    assert r.status_code == 200
    assert r.json() == []


def test_posts_rejects_bad_date_format(client):
    r = client.get("/api/posts", params={"date": "2026/09/14"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_date"


def test_posts_requires_date(client):
    assert client.get("/api/posts").status_code == 422


def test_posts_newest_first(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (3999000, "newer", "2026-09-14", "done", now_iso(), now_iso()),
        )
    tids = [p["tid"] for p in client.get("/api/posts", params={"date": "2026-09-14"}).json()]
    assert tids == [3999000, 3986000]

# tests/test_api.py  （追加）
def test_delete_removes_row(client, seed):
    assert client.delete(f"/api/posts/{seed}").status_code == 204
    assert client.get("/api/posts", params={"date": "2026-09-14"}).json() == []


def test_delete_is_hard_in_db(client, seed, db):
    """ADR-7：行必须真的消失。"""
    client.delete(f"/api/posts/{seed}")
    n = db.read().execute("SELECT COUNT(*) FROM posts WHERE tid=?", (seed,)).fetchone()[0]
    assert n == 0


def test_delete_missing_returns_404(client):
    r = client.delete("/api/posts/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_beacon_delete_works(client, seed):
    """sendBeacon 只能发 POST，语义必须等价于 DELETE。"""
    assert client.post(f"/api/posts/{seed}/delete").status_code == 204
    assert client.get("/api/posts", params={"date": "2026-09-14"}).json() == []


def test_beacon_delete_missing_returns_404(client):
    assert client.post("/api/posts/999999/delete").status_code == 404


def test_delete_removes_date_when_last_post_gone(client, seed):
    """删掉某日最后一帖后，该日期应从 /api/dates 消失。"""
    assert client.get("/api/dates").json() == [{"date": "2026-09-14", "count": 1}]
    client.delete(f"/api/posts/{seed}")
    assert client.get("/api/dates").json() == []


def test_delete_twice_second_is_404(client, seed):
    assert client.delete(f"/api/posts/{seed}").status_code == 204
    assert client.delete(f"/api/posts/{seed}").status_code == 404
