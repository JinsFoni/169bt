# tests/test_db.py
import sqlite3
import threading

import pytest

from bt169.db import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


def _insert(conn, key):
    conn.execute(
        "INSERT INTO settings(key,value,encrypted,updated_at)"
        " VALUES(?,?,0,'2026-01-01T00:00:00+08:00')",
        (key, "v"),
    )


def test_migrate_creates_tables(db):
    names = {
        r[0] for r in db.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"posts", "settings", "forum_session", "panel_session"} <= names


def test_migrate_is_idempotent(db):
    assert db.migrate() == SCHEMA_VERSION


def test_wal_enabled(db):
    assert db.read().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_set(db):
    assert db.read().execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_posts_has_no_deleted_at(db):
    """硬删除（ADR-7）：schema 不应有软删除列。"""
    cols = {r[1] for r in db.read().execute("PRAGMA table_info(posts)")}
    assert "deleted_at" not in cols
    assert {"tid", "code", "ed2k", "post_date", "status"} <= cols


def test_posts_status_is_not_null(db):
    """status 由应用层校验；SQLite 无枚举，靠 VALID_STATUSES。
    ``PRAGMA table_info`` 的列：cid, name, type, notnull, dflt_value, pk。
    """
    info = {r[1]: r for r in db.read().execute("PRAGMA table_info(posts)")}
    assert info["status"][3] == 1          # notnull


def test_migrate_does_not_break_transactions(db):
    """★ 回归：`executescript` 会隐式提交，迁移后 `write()` 必须仍可用。"""
    with db.write() as conn:
        _insert(conn, "after-migrate")
    assert db.read().execute(
        "SELECT COUNT(*) FROM settings WHERE key='after-migrate'"
    ).fetchone()[0] == 1


def test_write_context_commits(db):
    with db.write() as conn:
        _insert(conn, "k")
    row = db.read().execute("SELECT value FROM settings WHERE key='k'").fetchone()
    assert row[0] == "v"


def test_write_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            _insert(conn, "x")
            raise RuntimeError("boom")
    n = db.read().execute("SELECT COUNT(*) FROM settings WHERE key='x'").fetchone()[0]
    assert n == 0


def test_concurrent_writes_are_serialized(db):
    """实测：两连接同时 BEGIN IMMEDIATE → 'database is locked'。
    单写连接 + 锁必须让并发写全部成功。"""
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            with db.write() as conn:
                _insert(conn, f"k{n}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    n = db.read().execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    assert n == 20


def test_read_connection_is_per_thread(db):
    main = db.read()
    other: list[sqlite3.Connection] = []
    t = threading.Thread(target=lambda: other.append(db.read()))
    t.start()
    t.join()
    assert other[0] is not main


def test_reader_returns_same_conn_within_thread(db):
    assert db.read() is db.read()


def test_close_is_idempotent(db):
    db.close()
    db.close()


# ------------------------------------------------------------ 迁移链（逐版本）


def test_migration_chain_preserves_data(tmp_path):
    """★ 从 v1 逐级升到最新，数据必须一路保留。

    这类测试是**必需的**：真实库已有数据，迁移写错（例如
    ``ALTER TABLE`` 失败、或漏写 ``user_version``）会在生产库里
    才暴露。这里把每一级都真实走一遍。
    """
    from bt169.db import _MIGRATIONS_DIR

    path = tmp_path / "chain.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    scripts = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    assert len(scripts) >= 3

    # 只应用第一级，然后塞一行
    conn.executescript(scripts[0].read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version=1")
    conn.execute(
        "INSERT INTO posts(tid,title,post_date,status,retry_count,"
        " created_at,updated_at) VALUES(1,'老帖','2026-09-14','done',0,'t','t')"
    )
    conn.commit()
    conn.close()

    # 逐级升到最新
    d = Database(path)
    try:
        assert d.migrate() == SCHEMA_VERSION
        row = d.read().execute("SELECT * FROM posts WHERE tid=1").fetchone()
        assert row["title"] == "老帖"
        assert row["cover_local"] is None      # 新列默认为 NULL
        assert row["detail_local"] is None
    finally:
        d.close()


def test_migration_versions_are_unique_and_contiguous():
    """★ 迁移文件版本号必须唯一且从 1 连续。

    两个真实故障模式：

    1. **版本号重复**（``002_a.sql`` + ``002_b.sql``）：``migrate()`` 用
       ``version <= current`` 跳过，所以第二个文件会被**永久跳过**，
       而且不报错——schema 静默缺一块。
    2. **版本号不连续**：``_user_version`` 会跳号，``SCHEMA_VERSION``
       与实际不符，测试断言失去意义。
    """
    versions = [v for v, _ in Database._discover()]
    assert versions == list(range(1, len(versions) + 1)), (
        f"迁移版本号必须唯一且从 1 连续，实际 {versions}"
    )
    assert versions[-1] == SCHEMA_VERSION, (
        f"SCHEMA_VERSION={SCHEMA_VERSION} 与最后一个迁移文件 {versions[-1]} 不符"
    )


def test_alter_table_migrations_are_not_reapplied(tmp_path):
    """★ 回归：``ALTER TABLE ADD COLUMN`` 重复执行会报 duplicate column。

    这正是 ``test_every_migration_bumps_version`` 要防的故障——
    这里直接验证「连跑三次 migrate 不崩」。
    """
    d = Database(tmp_path / "thrice.db")
    try:
        assert d.migrate() == SCHEMA_VERSION
        assert d.migrate() == SCHEMA_VERSION
        assert d.migrate() == SCHEMA_VERSION
    finally:
        d.close()
