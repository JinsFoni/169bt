# tests/test_repo_archive.py
import pytest

from bt169.db import Database
from bt169.repo.archive import ArchiveRepo


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield ArchiveRepo(db), db
    db.close()


def insert(db, tid, post_date, status="done"):
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,'2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')",
            (tid, f"t{tid}", post_date, status),
        )


def test_empty(repo):
    ar, _ = repo
    assert ar.list_dates() == []
    assert ar.latest_date() is None


def test_list_dates_ascending_with_counts(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-10")
    insert(db, 3, "2026-09-14")
    got = ar.list_dates()
    assert [d.date for d in got] == ["2026-09-10", "2026-09-14"]
    assert [d.count for d in got] == [2, 1]


def test_only_done_status_is_browsable(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10", status="done")
    insert(db, 2, "2026-09-10", status="pending")
    insert(db, 3, "2026-09-10", status="nolink")
    assert ar.count_for("2026-09-10") == 1


def test_neighbour_skips_empty_gaps(repo):
    """C-10：日期跳转必须跳过无帖日期。"""
    ar, db = repo
    for tid, d in enumerate(["2026-09-01", "2026-09-05", "2026-09-14"], start=1):
        insert(db, tid, d)
    assert ar.neighbour("2026-09-05", -1) == "2026-09-01"
    assert ar.neighbour("2026-09-05", +1) == "2026-09-14"


def test_neighbour_boundaries_return_none(repo):
    """W-12：首/尾日期时对应按钮应禁用 → 返回 None。"""
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    assert ar.neighbour("2026-09-10", -1) is None
    assert ar.neighbour("2026-09-14", +1) is None


def test_neighbour_unknown_date(repo):
    """未知日期的语义：返回**严格**早于/晚于该日期的第一个有帖日期。

    前端不会从无效日期导航（它会先回退到最新日期），所以这是退化情况，
    但语义必须一致——否则“上一天”按钮会突然跳到无关位置。
    """
    ar, db = repo
    insert(db, 1, "2026-09-10")
    assert ar.neighbour("1999-01-01", -1) is None          # 比最早的还早
    assert ar.neighbour("1999-01-01", +1) == "2026-09-10"  # 比它晚的第一个
    assert ar.neighbour("2099-01-01", +1) is None          # 比最晚的还晚
    assert ar.neighbour("2099-01-01", -1) == "2026-09-10"  # 比它早的第一个


def test_neighbour_rejects_bad_direction(repo):
    ar, _ = repo
    with pytest.raises(ValueError, match="direction"):
        ar.neighbour("2026-09-10", 0)


def test_latest_date(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    assert ar.latest_date() == "2026-09-14"


def test_hard_deleted_row_disappears_from_dates(repo):
    """硬删除（ADR-7）：删行后该日期若空则应从列表消失。"""
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    with db.write() as c:
        c.execute("DELETE FROM posts WHERE tid=1")
    assert [d.date for d in ar.list_dates()] == ["2026-09-14"]


def test_date_count_to_dict(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    assert ar.list_dates()[0].to_dict() == {"date": "2026-09-10", "count": 1}
