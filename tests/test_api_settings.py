# tests/test_api_settings.py
import pytest

from bt169.config import PLACEHOLDER


def test_get_settings_empty(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {s: {} for s in ("site", "basic", "proxy", "emby", "tg")}


def test_put_then_get_roundtrip(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh"},
    })
    assert r.status_code == 200
    assert r.json()["saved"] == ["username"]
    assert client.get("/api/settings").json()["site"]["username"] == "ymxh"


def test_put_then_get_masks_secret(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"password": "hunter2"},
    })
    assert r.json()["saved"] == ["password"]
    got = client.get("/api/settings").json()
    assert got["site"]["password"] == PLACEHOLDER


def test_basic_section_roundtrip(client):
    """「基础设置」目前只有访问密码——它由认证模块以**哈希**存储，
    不经这个 KV 接口回读（见 ADR-17）。这里用一个普通字段占位验证分区可用。"""
    client.put("/api/settings", json={
        "section": "basic", "values": {"theme": "dark"},
    })
    assert client.get("/api/settings").json()["basic"]["theme"] == "dark"


def test_secret_never_returned_in_plaintext(client):
    client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh", "password": "s3cr3t-pw"},
    })
    body = client.get("/api/settings").text
    assert "s3cr3t-pw" not in body
    assert client.get("/api/settings").json()["site"]["password"] == PLACEHOLDER


def test_non_secret_returned_plaintext(client):
    client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh"},
    })
    assert client.get("/api/settings").json()["site"]["username"] == "ymxh"


def test_placeholder_means_unchanged(client, db, box):
    """前端回传未修改的密码 → 后端保留原值，不写占位符。

    ★ 种子数据必须用**带分区前缀**的键，与 API 写入的一致；
    否则断言会因为读到空值而**空洞通过**。
    """
    from bt169.repo.settings import SettingsRepo
    sr = SettingsRepo(db, box)
    sr.put("site.password", "real-secret")

    r = client.put("/api/settings", json={
        "section": "site", "values": {"password": PLACEHOLDER, "username": "new"},
    })
    assert r.json()["skipped"] == ["password"]
    assert sr.get("site.password") == "real-secret"
    assert sr.get("site.username") == "new"


def test_rss_url_is_cleaned_on_save(client, db, box):
    """实测：RSS 匿名可用 → auth 参数必须被剥掉。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={
        "section": "site",
        "values": {"rss_url": "https://169bt.com/forum.php?mod=rss&fid=192&auth=deadbeef"},
    })
    assert SettingsRepo(db, box).get("site.rss_url") == "https://169bt.com/forum.php?fid=192"


def test_rss_url_without_fid_rejected(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"rss_url": "https://169bt.com/forum.php?mod=rss"},
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_rss_url"


def test_unknown_section_rejected(client):
    r = client.put("/api/settings", json={"section": "nope", "values": {"a": "b"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_section"


def test_secret_fields_are_encrypted_at_rest(client, db):
    """★ 端到端：经 API 存密码 → 库里必须是密文。"""
    client.put("/api/settings", json={
        "section": "site", "values": {"password": "plaintext-check"},
    })
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.password'"
    ).fetchone()
    assert row["encrypted"] == 1
    assert "plaintext-check" not in row["value"]


def test_sections_are_isolated(client):
    """ADR-18：一次只改一个分区，其他分区不受影响。"""
    client.put("/api/settings", json={"section": "tg", "values": {"chat_id": "1"}})
    client.put("/api/settings", json={"section": "emby", "values": {"url": "http://e"}})
    got = client.get("/api/settings").json()
    assert got["tg"] == {"chat_id": "1"}
    assert got["emby"] == {"url": "http://e"}
    assert got["proxy"] == {}


def test_missing_body_fields(client):
    assert client.put("/api/settings", json={}).status_code == 422


def test_delete_setting(client, db, box):
    """清空某键（值为空字符串）→ 从库中移除。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={"section": "emby", "values": {"url": "http://e"}})
    client.put("/api/settings", json={"section": "emby", "values": {"url": ""}})
    assert SettingsRepo(db, box).get("emby.url") is None


def test_same_field_name_in_two_sections_does_not_collide(client, db, box):
    """★ 回归：`password` 在「站点」与「代理」都存在。
    没加分区前缀时，写代理密码会静默抹掉论坛密码。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={
        "section": "site", "values": {"password": "forum-pw"},
    })
    client.put("/api/settings", json={
        "section": "proxy", "values": {"password": "proxy-pw"},
    })

    sr = SettingsRepo(db, box)
    assert sr.get("site.password") == "forum-pw"    # 没被覆盖
    assert sr.get("proxy.password") == "proxy-pw"

    got = client.get("/api/settings").json()
    assert got["site"]["password"] == PLACEHOLDER
    assert got["proxy"]["password"] == PLACEHOLDER
