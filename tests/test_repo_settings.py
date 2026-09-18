# tests/test_repo_settings.py
import pytest

from bt169.config import PLACEHOLDER
from bt169.crypto import SecretBox
from bt169.db import Database
from bt169.repo.settings import SettingsRepo

KEY = b"\x07" * 32


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield SettingsRepo(db, SecretBox(KEY)), db
    db.close()


def test_put_get_roundtrip(repo):
    sr, _ = repo
    sr.put("site.rss_url", "https://169bt.com/forum.php?fid=192")
    assert sr.get("site.rss_url") == "https://169bt.com/forum.php?fid=192"


def test_get_default(repo):
    sr, _ = repo
    assert sr.get("missing") is None
    assert sr.get("missing", "d") == "d"


def test_secret_is_encrypted_at_rest(repo):
    """密钥字段落库必须是密文，且标记 encrypted=1。"""
    sr, db = repo
    sr.put("site.password", "hunter2")
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.password'"
    ).fetchone()
    assert row["encrypted"] == 1
    assert "hunter2" not in row["value"]
    assert sr.get("site.password") == "hunter2"          # 读出来是明文


def test_non_secret_stays_plaintext(repo):
    sr, db = repo
    sr.put("site.rss_url", "x")
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.rss_url'"
    ).fetchone()
    assert row["encrypted"] == 0
    assert row["value"] == "x"


def test_all_secret_fields_are_encrypted(repo):
    sr, db = repo
    for k in ("site.password", "tg.token", "emby.apikey"):
        sr.put(k, f"v-{k}")
        row = db.read().execute(
            "SELECT value, encrypted FROM settings WHERE key=?", (k,)
        ).fetchone()
        assert row["encrypted"] == 1, k
        assert f"v-{k}" not in row["value"], k


def test_non_secret_suffix_is_not_encrypted(repo):
    """`panel_password` 最后一段不是 `password` → 不应被当作密钥字段。"""
    sr, db = repo
    sr.put("basic.panel_password_hash", "x")
    row = db.read().execute(
        "SELECT encrypted FROM settings WHERE key='basic.panel_password_hash'"
    ).fetchone()
    assert row["encrypted"] == 0


def test_get_all_returns_plaintext_by_default(repo):
    sr, _ = repo
    sr.put("site.password", "hunter2")
    sr.put("site.rss_url", "x")
    assert sr.get_all() == {"site.password": "hunter2", "site.rss_url": "x"}


def test_get_all_masked_hides_secrets(repo):
    """脱敏读取：密钥字段变占位符，非密钥字段照旧。"""
    sr, _ = repo
    sr.put("site.password", "hunter2")
    sr.put("site.rss_url", "x")
    got = sr.get_all(masked=True)
    assert got["site.password"] == PLACEHOLDER
    assert got["site.rss_url"] == "x"
    assert "hunter2" not in str(got)


def test_put_many_skips_placeholder(repo):
    """提交时等于占位符 → 视为「未修改」，保留旧值。"""
    sr, _ = repo
    sr.put("site.password", "old-secret")
    skipped = sr.put_many({"site.password": PLACEHOLDER, "site.rss_url": "new"})
    assert skipped == ["site.password"]
    assert sr.get("site.password") == "old-secret"
    assert sr.get("site.rss_url") == "new"


def test_put_many_updates_real_value(repo):
    sr, _ = repo
    sr.put("site.password", "old")
    sr.put_many({"site.password": "new"})
    assert sr.get("site.password") == "new"


def test_put_overwrites(repo):
    sr, _ = repo
    sr.put("site.username", "a")
    sr.put("site.username", "b")
    assert sr.get("site.username") == "b"


def test_sections_do_not_collide_at_repo_level(repo):
    """★ 回归：两个分区的同名 key 必须互不影响。"""
    sr, _ = repo
    sr.put("site.password", "forum")
    sr.put("proxy.password", "proxy")
    assert sr.get("site.password") == "forum"
    assert sr.get("proxy.password") == "proxy"


def test_put_updates_timestamp(repo):
    sr, db = repo
    sr.put("site.k", "a")
    first = db.read().execute(
        "SELECT updated_at FROM settings WHERE key='site.k'"
    ).fetchone()[0]
    sr.put("site.k", "b")
    second = db.read().execute(
        "SELECT updated_at FROM settings WHERE key='site.k'"
    ).fetchone()[0]
    assert second >= first


def test_delete(repo):
    sr, _ = repo
    sr.put("site.k", "a")
    sr.delete("site.k")
    assert sr.get("site.k") is None


def test_wrong_key_raises_on_read(repo, tmp_path):
    """换密钥后读旧密文必须报错，而不是返回垃圾。"""
    from bt169.crypto import DecryptError
    sr, db = repo
    sr.put("site.password", "hunter2")
    other = SettingsRepo(db, SecretBox(b"\x08" * 32))
    with pytest.raises(DecryptError):
        other.get("site.password")
