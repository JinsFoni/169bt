# tests/conftest.py
import pytest
from fastapi.testclient import TestClient

from bt169.crypto import SecretBox
from bt169.db import Database

TEST_KEY = b"\x09" * 32


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


@pytest.fixture
def box():
    return SecretBox(TEST_KEY)


@pytest.fixture
def client(db, box):
    from bt169.api.app import create_app
    app = create_app(db, box=box, ui_dir=None)   # None = 不挂静态，只测 API
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed(db):
    """往库里塞一条可浏览的帖子，返回 tid。"""
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,code,actress,release_date,size,"
            " cover_img,detail_img,ed2k,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (3986000, "START-624 本庄鈴", "START-624", "本庄鈴", "2026-09-17", "7GB",
             "https://img/c.jpg", "https://img/d.jpg",
             "ed2k://|file|x.mkv|1|AA|/", "2026-09-14", "done",
             now_iso(), now_iso()),
        )
    return 3986000
