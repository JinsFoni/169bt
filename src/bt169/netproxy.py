"""应用内代理：从 ``proxy.*`` 设置构造各 HTTP 客户端需要的代理参数。

## 为什么存在

设置面板的「网络代理」分区（``proxy.type/host/port/username/password``）
早就存在，但过去**没有任何代码读它**——采集与 TG 全部直连。
本模块是唯一读这组设置的地方，产出两种客户端各需要的形态：

- httpx（论坛抓取 / RSS / 图床 / TG Bot API）→ ``proxy=`` URL 字符串
- Telethon（TG MTProto）→ ``proxy=`` dict（python-socks 签名）

## 范围（刻意的）

代理**只**作用于采集链路（论坛抓取、RSS、图床下载、论坛登录）
与 TG（Bot API + MTProto）。其余流量一律直连：

- Emby（局域网，走代理反而绕远甚至失败）——``emby.py`` 自己
  ``trust_env=False``，与本模块无关
- 健康探针 / 本机回环 —— 无代理客户端不涉及

## 环境变量是**被禁用**的

所有经本模块构造的客户端都带 ``trust_env=False``：compose 里写的
``HTTPS_PROXY`` / ``HTTP_PROXY`` 一律**不参与**。代理只有应用设置
这一个入口——否则两层配置叠加，出了网络问题没法排查。

## 失败哲学

解析失败（host/port 缺失、端口非数字、协议不认识）一律**当作未配置
代理**返回 ``None``，绝不抛异常——代理配置坏了不该让采集或 TG 直接
挂掉，顶多回到直连（直连失败会有各自的正常报错路径）。
"""

from __future__ import annotations

from typing import Any

from bt169 import config

__all__ = [
    "resolve_proxy",
    "httpx_kwargs",
    "telethon_proxy",
    "ProxyConfigError",
]

#: 合法的代理协议（与设置面板下拉选项一致）。
_SCHEMES = frozenset({"http", "https", "socks5"})


class ProxyConfigError(ValueError):
    """代理设置非法。

    仅由 :func:`_validate` 在**校验入口**抛出；对外的三个函数
    （``resolve_proxy`` / ``httpx_kwargs`` / ``telethon_proxy``）把
    一切解析失败吞成 ``None``——调用方不该为「代理配置坏了」写
    特殊处理。
    """


def _validate(settings: Any) -> tuple[str, str, int, str, str] | None:
    """读 ``proxy.*`` 设置并校验。

    Returns:
        ``(scheme, host, port, username, password)``，未配置或非法 → ``None``。
    """
    sk = config.section_key
    try:
        ptype = (settings.get(sk("proxy", "type")) or "").strip().lower()
        host = (settings.get(sk("proxy", "host")) or "").strip()
        port_raw = (settings.get(sk("proxy", "port")) or "").strip()
        username = settings.get(sk("proxy", "username")) or ""
        password = settings.get(sk("proxy", "password")) or ""
    except Exception:  # noqa: BLE001 — 库坏了/解密失败 = 未配置
        return None

    if ptype == "none" or not host or not port_raw:
        return None
    if ptype not in _SCHEMES:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return ptype, host, port, username, password


def resolve_proxy(settings: Any) -> str | None:
    """``proxy.*`` 设置 → httpx 的 ``proxy=`` URL。

    密码里的特殊字符（``@ : / # ? %`` 等）按 RFC 3986 percent-encode，
    否则 ``httpx`` 解析 URL 时会把它们当 URL 语法。

    Returns:
        形如 ``socks5://user:pw@host:port``；未配置/非法 → ``None``。
    """
    parsed = _validate(settings)
    if parsed is None:
        return None
    scheme, host, port, username, password = parsed
    userinfo = ""
    if username or password:
        userinfo = f"{_uri_quote(username)}:{_uri_quote(password)}@"
    return f"{scheme}://{userinfo}{host}:{port}"


def _uri_quote(value: str) -> str:
    """URI userinfo 段的转义（``safe=""`` → ``@ : / ? # %`` 全编码）。"""
    from urllib.parse import quote

    return quote(value, safe="")


def httpx_kwargs(settings: Any) -> dict[str, Any]:
    """httpx 客户端的代理构造参数。

    ★ 无论是否配置代理，返回值都含 ``trust_env=False``——环境变量
    一律不参与（见模块 docstring「环境变量是被禁用的」）。

    用法::

        client = httpx.Client(timeout=30, **httpx_kwargs(settings))
    """
    proxy = resolve_proxy(settings)
    kw: dict[str, Any] = {"trust_env": False}
    if proxy is not None:
        kw["proxy"] = proxy
    return kw


def telethon_proxy(settings: Any) -> dict[str, Any] | None:
    """``proxy.*`` 设置 → Telethon 的 ``proxy=`` dict。

    键名是 python-socks 的 ``Proxy.create()`` 签名（Telethon 1.x 内部
    就是这么消费的）：``proxy_type`` 用小写字符串（``"http"`` /
    ``"socks5"``，Telethon 的 ``_parse_proxy`` 原生支持）。

    ``rdns=True``（远端 DNS 解析）：域名在代理出口解析，本地 DNS
    污染不影响 TG 连接目标。

    Returns:
        dict；未配置/非法 → ``None``。
    """
    parsed = _validate(settings)
    if parsed is None:
        return None
    scheme, host, port, username, password = parsed
    out: dict[str, Any] = {
        "proxy_type": scheme,
        "addr": host,
        "port": port,
        "rdns": True,
    }
    if username or password:
        out["username"] = username
        out["password"] = password
    return out
