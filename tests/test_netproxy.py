"""netproxy：应用内代理设置的解析与注入。

范围（本任务的目标）：
- ``proxy.*`` 设置 → httpx ``proxy=`` URL / Telethon ``proxy=`` dict 两种形态
- ``trust_env=False`` 是**硬规则**：环境变量（compose 里配的 HTTPS_PROXY 等）
  一律不参与，代理只认应用设置。
"""

from __future__ import annotations

import pytest

from bt169 import config
from bt169.repo.settings import SettingsRepo


# ---------------------------------------------------------------- 解析


def test_none_when_unset(db, box):
    """未配置 → None（不代理）。"""
    from bt169.netproxy import resolve_proxy

    assert resolve_proxy(SettingsRepo(db, box)) is None


def test_full_url_http(db, box):
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "192.168.0.102")
    sr.put(config.section_key("proxy", "port"), "7890")
    assert resolve_proxy(sr) == "http://192.168.0.102:7890"


def test_full_url_with_auth(db, box):
    """用户名/密码 → URL 编码后进 URL userinfo。"""
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "8080")
    sr.put(config.section_key("proxy", "username"), "user")
    sr.put(config.section_key("proxy", "password"), "p@ss:wo rd")
    assert resolve_proxy(sr) == "http://user:p%40ss%3Awo%20rd@127.0.0.1:8080"


def test_full_url_socks5(db, box):
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "socks5")
    sr.put(config.section_key("proxy", "host"), "10.0.0.2")
    sr.put(config.section_key("proxy", "port"), "1080")
    assert resolve_proxy(sr) == "socks5://10.0.0.2:1080"


def test_none_when_type_none(db, box):
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "none")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "7890")
    assert resolve_proxy(sr) is None


@pytest.mark.parametrize("missing", ["host", "port"])
def test_none_when_missing_part(db, box, missing):
    """host 或 port 缺失 → 视为未配置，而不是半吊子代理。"""
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    if missing != "host":
        sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    if missing != "port":
        sr.put(config.section_key("proxy", "port"), "7890")
    assert resolve_proxy(sr) is None


def test_none_when_bad_port(db, box):
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "not-a-number")
    assert resolve_proxy(sr) is None


def test_none_when_unknown_type(db, box):
    from bt169.netproxy import resolve_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "gopher")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "7890")
    assert resolve_proxy(sr) is None


# ---------------------------------------------------------------- 注入


def test_httpx_kwargs_direct(db, box):
    """未配置代理 → 仍然强制 ``trust_env=False``。"""
    from bt169.netproxy import httpx_kwargs

    kw = httpx_kwargs(SettingsRepo(db, box))
    assert kw == {"trust_env": False}


def test_httpx_kwargs_proxy(db, box):
    from bt169.netproxy import httpx_kwargs

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "7890")
    kw = httpx_kwargs(sr)
    assert kw == {"proxy": "http://127.0.0.1:7890", "trust_env": False}


def test_httpx_client_mounts_use_proxy(db, box):
    """端到端：httpx.Client(**kw) 的传输层真的挂上了 HTTPProxy。

    httpx 0.28 的实现：``proxy=`` 参数的传输层挂在 ``_mounts['all://']``，
    ``_transport`` 本身仍是直连池。
    """
    import httpx

    from bt169.netproxy import httpx_kwargs

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "7890")
    c = httpx.Client(**httpx_kwargs(sr))
    transports = [c._transport, *c._mounts.values()]
    proxies = [
        t._pool for t in transports
        if t is not None and type(t._pool).__name__ == "HTTPProxy"
    ]
    assert len(proxies) == 1
    assert b"127.0.0.1" in bytes(proxies[0]._proxy_url.host)


def test_telethon_proxy_dict(db, box):
    from bt169.netproxy import telethon_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "192.168.0.102")
    sr.put(config.section_key("proxy", "port"), "7890")
    sr.put(config.section_key("proxy", "username"), "u")
    sr.put(config.section_key("proxy", "password"), "p")
    assert telethon_proxy(sr) == {
        "proxy_type": "http",
        "addr": "192.168.0.102",
        "port": 7890,
        "username": "u",
        "password": "p",
        "rdns": True,
    }


def test_telethon_proxy_dict_minimal(db, box):
    from bt169.netproxy import telethon_proxy

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("proxy", "type"), "socks5")
    sr.put(config.section_key("proxy", "host"), "10.0.0.2")
    sr.put(config.section_key("proxy", "port"), "1080")
    assert telethon_proxy(sr) == {
        "proxy_type": "socks5",
        "addr": "10.0.0.2",
        "port": 1080,
        "rdns": True,
    }


def test_telethon_proxy_none(db, box):
    from bt169.netproxy import telethon_proxy

    assert telethon_proxy(SettingsRepo(db, box)) is None


# ---------------------------------------------------------------- 客户端构造点


def test_forum_client_uses_settings_proxy(db, box):
    """ForumClient 默认构造 → 从设置读代理。"""
    from bt169.source.forum import ForumClient

    settings = SettingsRepo(db, box)
    settings.put(config.section_key("proxy", "type"), "http")
    settings.put(config.section_key("proxy", "host"), "127.0.0.1")
    settings.put(config.section_key("proxy", "port"), "7890")
    fc = ForumClient(db=db)
    transports = [fc._client._transport, *fc._client._mounts.values()]
    pools = [t._pool for t in transports if t is not None]
    assert any(type(p).__name__ == "HTTPProxy" for p in pools)
    fc.close()


def test_forum_client_injects_transport(db, box):
    """测试注入 transport 时不需要 db 参数。"""
    import httpx

    from bt169.source.forum import ForumClient

    inner = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    fc = ForumClient(client=inner)
    assert fc._client is inner
    fc.close()


def test_telegram_route_client_uses_proxy(db, box):
    """TG Bot 客户端构造 → 从设置读代理。"""
    import httpx

    from bt169.api.routes.telegram import build_client
    from bt169.db import Database

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "token"), "123:abc")
    sr.put(config.section_key("tg", "chat_id"), "@me")
    sr.put(config.section_key("proxy", "type"), "http")
    sr.put(config.section_key("proxy", "host"), "127.0.0.1")
    sr.put(config.section_key("proxy", "port"), "7890")
    tc = build_client(sr)
    transports = [tc.http._transport, *tc.http._mounts.values()]
    assert any(
        t is not None and type(t._pool).__name__ == "HTTPProxy"
        for t in transports
    )
    tc.http.close()


def test_build_sender_uses_proxy(db, box, monkeypatch):
    """MTProto 发送器 → Telethon 客户端带 proxy dict。"""
    import bt169.mtpproto as m
    from bt169.repo.settings import SettingsRepo
    from tests.test_mtpproto import FakeMtp

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "api_id"), "12345")
    sr.put(config.section_key("tg", "api_hash"), "deadbeef")
    sr.put(config.section_key("session" if False else "tg", "session"), "1BaBhK0Q4Q04nPqrfYHkC5rDWXk")
    sr.put(config.section_key("proxy", "type"), "socks5")
    sr.put(config.section_key("proxy", "host"), "10.0.0.2")
    sr.put(config.section_key("proxy", "port"), "1080")

    captured = {}

    def fake_ctor(session, api_id, api_hash, proxy=None):
        captured.update(proxy=proxy)
        return FakeMtp()

    monkeypatch.setattr(m, "TelegramClient", fake_ctor)
    monkeypatch.setattr(m, "StringSession", lambda s: s)
    sender = m.build_sender(sr)
    assert captured["proxy"] == {
        "proxy_type": "socks5",
        "addr": "10.0.0.2",
        "port": 1080,
        "rdns": True,
    }
