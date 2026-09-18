import pytest
from bt169.config import (
    ConfigError, PLACEHOLDER, SECRET_FIELDS, SETTINGS_SECTIONS, is_secret,
    parse_rss_url, section_key,
)


def test_parse_rss_url_extracts_fid():
    fid, clean = parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=192")
    assert fid == "192"
    assert clean == "https://169bt.com/forum.php?fid=192"


def test_parse_rss_url_strips_auth():
    fid, clean = parse_rss_url(
        "https://169bt.com/forum.php?mod=rss&fid=192&auth=deadbeef&ver=2"
    )
    assert fid == "192"
    assert "auth" not in clean
    assert "ver" not in clean
    assert clean == "https://169bt.com/forum.php?fid=192"


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
