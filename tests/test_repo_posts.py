# tests/test_repo_posts.py
import pytest

from bt169.db import Database
from bt169.repo.posts import PostRepo

NOW = "2026-09-18T12:00:00+08:00"


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield PostRepo(db), db
    db.close()


def insert(db, tid, post_date="2026-09-14", status="done", code="START-624", **over):
    cols = {
        "tid": tid, "title": f"t{tid}", "post_date": post_date, "status": status,
        "code": code, "ed2k": "ed2k://|file|x.mkv|1|AA|/",
        "cover_img": "https://img/c.jpg", "detail_img": "https://img/d.jpg",
        "created_at": NOW, "updated_at": NOW, **over,
    }
    names = ", ".join(cols)
    ph = ", ".join("?" * len(cols))
    with db.write() as c:
        c.execute(f"INSERT INTO posts ({names}) VALUES ({ph})", tuple(cols.values()))


def test_list_by_date_only_browsable(repo):
    pr, db = repo
    insert(db, 1, status="done")
    insert(db, 2, status="pending")
    insert(db, 3, status="nolink")
    assert [p.tid for p in pr.list_by_date("2026-09-14")] == [1]


def test_list_by_date_newest_tid_first(repo):
    pr, db = repo
    insert(db, 100)
    insert(db, 300)
    insert(db, 200)
    assert [p.tid for p in pr.list_by_date("2026-09-14")] == [300, 200, 100]


def test_list_by_date_empty(repo):
    pr, _ = repo
    assert pr.list_by_date("2026-09-14") == []


def test_get(repo):
    pr, db = repo
    insert(db, 3986000)
    p = pr.get(3986000)
    assert p is not None and p.code == "START-624"
    assert pr.get(999) is None


def test_delete_is_hard(repo):
    """ADR-7：硬删除 —— 行必须真的消失，不留 deleted_at。"""
    pr, db = repo
    insert(db, 1)
    assert pr.delete(1) is True
    assert pr.get(1) is None
    n = db.read().execute("SELECT COUNT(*) FROM posts WHERE tid=1").fetchone()[0]
    assert n == 0


def test_delete_missing_returns_false(repo):
    pr, _ = repo
    assert pr.delete(999) is False


def test_delete_is_idempotent(repo):
    pr, db = repo
    insert(db, 1)
    assert pr.delete(1) is True
    assert pr.delete(1) is False


def test_set_status(repo):
    pr, db = repo
    insert(db, 1, status="pending")
    pr.set_status(1, "failed", error="boom", next_retry_at=NOW)
    p = pr.get(1)
    assert p.status == "failed"
    assert p.last_error == "boom"
    assert p.next_retry_at == NOW


def test_set_status_rejects_unknown(repo):
    pr, db = repo
    insert(db, 1)
    with pytest.raises(ValueError, match="状态"):
        pr.set_status(1, "bogus")


def test_set_status_clears_error_on_success(repo):
    pr, db = repo
    insert(db, 1, status="failed", last_error="boom")
    pr.set_status(1, "done")
    p = pr.get(1)
    assert p.status == "done"
    assert p.last_error is None
    assert p.next_retry_at is None


def test_set_emby(repo):
    pr, db = repo
    insert(db, 1)
    pr.set_emby(1, in_library=True, item_id="abc123", checked_at=NOW)
    p = pr.get(1)
    assert p.emby_status == "in_library"
    assert p.emby_item_id == "abc123"
    assert p.emby_checked == NOW


def test_set_emby_not_in_library(repo):
    pr, db = repo
    insert(db, 1)
    pr.set_emby(1, in_library=False, checked_at=NOW)
    p = pr.get(1)
    assert p.emby_status == "none"
    assert p.emby_item_id is None


def test_mark_tg_sent(repo):
    pr, db = repo
    insert(db, 1)
    assert pr.get(1).tg_sent_at is None
    pr.mark_tg_sent(1, NOW)
    assert pr.get(1).tg_sent_at == NOW


def test_count_by_status(repo):
    pr, db = repo
    insert(db, 1, status="done")
    insert(db, 2, status="done")
    insert(db, 3, status="pending")
    assert pr.count_by_status() == {"done": 2, "pending": 1}


def test_updated_at_bumped_on_write(repo):
    pr, db = repo
    insert(db, 1, status="pending")
    before = pr.get(1).updated_at
    pr.set_status(1, "done")
    assert pr.get(1).updated_at != before
