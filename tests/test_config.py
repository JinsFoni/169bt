import pytest
from urllib.parse import parse_qs, urlparse

from bt169.config import (
    ConfigError, PLACEHOLDER, SECRET_FIELDS, SETTINGS_SECTIONS, is_secret,
    parse_rss_url, section_key,
)


def test_parse_rss_url_extracts_fid():
    fid, clean = parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=192")
    assert fid == "192"
    # ★ 必须保留 mod=rss。丢掉它拿到的是**版块 HTML 页**，不是订阅：
    #   实测 forum.php?fid=192 → 57 KB HTML / 0 个 <item>，
    #        forum.php?mod=rss&fid=192 → 16 KB RSS / 20 个 <item>。
    assert clean == "https://169bt.com/forum.php?mod=rss&fid=192"


def test_parse_rss_url_cleaned_url_is_actually_a_feed():
    """钉住清洗结果真的能当订阅用（防止再次把 mod 剥掉）。"""
    _, clean = parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=192")
    qs = parse_qs(urlparse(clean).query)
    assert qs.get("mod") == ["rss"]
    assert qs.get("fid") == ["192"]


def test_parse_rss_url_strips_auth():
    fid, clean = parse_rss_url(
        "https://169bt.com/forum.php?mod=rss&fid=192&auth=deadbeef&ver=2"
    )
    assert fid == "192"
    assert "auth" not in clean
    assert "ver" not in clean
    assert clean == "https://169bt.com/forum.php?mod=rss&fid=192"


def test_parse_rss_url_canonicalizes_rss_php():
    """``rss.php`` 是 Discuz 的另一种写法；统一成 canonical 形式。

    实测 ``rss.php?fid=192`` 被 WAF 拦下返回 404，而 canonical 形式正常。
    """
    fid, clean = parse_rss_url("https://169bt.com/rss.php?fid=192")
    assert fid == "192"
    assert clean == "https://169bt.com/forum.php?mod=rss&fid=192"


def test_parse_rss_url_adds_mod_when_absent():
    """用户只贴版块 URL 时补上 mod=rss（这个设置项就是「订阅链接」）。"""
    fid, clean = parse_rss_url("https://169bt.com/forum.php?fid=192")
    assert fid == "192"
    assert clean == "https://169bt.com/forum.php?mod=rss&fid=192"


def test_parse_rss_url_rejects_non_rss_mod():
    """mod 是别的值 → 不是订阅链接，拒绝（不能静默改写成 rss）。"""
    with pytest.raises(ConfigError, match="rss"):
        parse_rss_url("https://169bt.com/forum.php?mod=forumdisplay&fid=192")


def test_parse_rss_url_rejects_missing_fid():
    with pytest.raises(ConfigError, match="fid"):
        parse_rss_url("https://169bt.com/forum.php?mod=rss")


def test_parse_rss_url_rejects_non_numeric_fid():
    with pytest.raises(ConfigError, match="数字"):
        parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=abc")


def test_parse_rss_url_rejects_empty():
    with pytest.raises(ConfigError):
        parse_rss_url("")


def test_parse_rss_url_rejects_relative():
    with pytest.raises(ConfigError, match="http"):
        parse_rss_url("/forum.php?mod=rss&fid=192")


def test_secret_fields_cover_credentials():
    assert {"password", "token", "apikey"} <= SECRET_FIELDS


def test_is_secret_handles_namespaced_keys():
    assert is_secret("site.password")
    assert is_secret("proxy.password")
    assert is_secret("tg.token")
    assert not is_secret("site.username")
    assert not is_secret("basic.panel_password")


def test_section_key_namespacing():
    assert section_key("site", "password") == "site.password"
    assert section_key("proxy", "password") == "proxy.password"
    assert section_key("site", "password") != section_key("proxy", "password")


def test_settings_sections_site_first():
    assert SETTINGS_SECTIONS[0] == "site"
    assert set(SETTINGS_SECTIONS) == {"site", "basic", "proxy", "emby", "tg"}


def test_placeholder_is_eight_dots():
    assert PLACEHOLDER == "\u2022" * 8
