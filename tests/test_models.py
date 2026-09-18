# tests/test_models.py
import sqlite3

import pytest

from bt169.models import BROWSABLE_STATUSES, VALID_STATUSES, Post, PostDTO

BASE = {
    "tid": 3986000, "title": "t", "code": "START-624", "actress": "本庄鈴",
    "release_date": "2026-09-17", "size": "7GB",
    "cover_img": "https://img/c.jpg", "detail_img": "https://img/d.jpg",
    "ed2k": "ed2k://|file|x.mkv|1|AA|/", "post_date": "2026-09-14",
    "post_time": None, "status": "done", "retry_count": 0,
    "last_error": None, "next_retry_at": None, "emby_status": None,
    "emby_item_id": None, "emby_checked": None, "tg_sent_at": None,
    "created_at": "2026-09-14T10:00:00+08:00",
    "updated_at": "2026-09-14T10:00:00+08:00",
}


def row(**over):
    """用**真实迁移的 schema** 造一行，避免手写列名漂移。

    用 ``:memory:`` 而不是临时文件：快，且不泄漏临时目录。
    """
    from bt169.db import _MIGRATIONS_DIR

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((_MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8"))

    data = {**BASE, **over}
    cols = ", ".join(data)
    ph = ", ".join("?" * len(data))
    conn.execute(f"INSERT INTO posts ({cols}) VALUES ({ph})", tuple(data.values()))
    return conn.execute("SELECT * FROM posts").fetchone()


def test_from_row():
    p = Post.from_row(row())
    assert p.tid == 3986000
    assert p.cover_img == "https://img/c.jpg"
    assert p.status == "done"


def test_from_row_covers_every_column():
    """Post 必须能吸收 schema 的**全部**列（漏一列就 TypeError）。"""
    p = Post.from_row(row())
    assert p.tg_sent_at is None
    assert p.emby_checked is None


def test_dto_renames_cover_and_detail():
    """ADR-11：前端消费 cover/detail，不是 cover_img/detail_img。"""
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    assert data["cover"] == "https://img/c.jpg"
    assert data["detail"] == "https://img/d.jpg"
    assert "cover_img" not in data
    assert "detail_img" not in data


def test_dto_excludes_internal_fields():
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    for leaked in ("retry_count", "last_error", "next_retry_at", "emby_item_id",
                   "emby_checked", "emby_status", "created_at", "updated_at"):
        assert leaked not in data, f"{leaked} 不应暴露给前端"


def test_dto_exact_key_set():
    """锁定契约（ADR-16）：键集合变化必须是显式决定。"""
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    assert set(data) == {
        "tid", "title", "code", "actress", "release_date", "size",
        "cover", "detail", "ed2k", "post_date", "status", "emby_in_library",
    }


def test_dto_emby_flag():
    p = Post.from_row(row())
    assert PostDTO.from_post(p).to_dict()["emby_in_library"] is False
    assert PostDTO.from_post(p, emby_in_library=True).to_dict()["emby_in_library"] is True


def test_valid_statuses():
    assert VALID_STATUSES == {"pending", "thanked", "done", "failed", "nolink"}


def test_browsable_is_only_done():
    assert BROWSABLE_STATUSES == {"done"}


def test_dto_handles_null_ed2k():
    data = PostDTO.from_post(Post.from_row(row(ed2k=None, status="nolink"))).to_dict()
    assert data["ed2k"] is None
    assert data["status"] == "nolink"


def test_post_is_frozen():
    p = Post.from_row(row())
    with pytest.raises(Exception):
        p.tid = 1  # type: ignore[misc]
