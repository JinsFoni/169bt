"""路径、常量与配置解析。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = [
    "ConfigError", "PROJECT_ROOT", "DATA_DIR", "DB_PATH", "IMAGE_DIR", "UI_DIR",
    "SECRET_FIELDS", "SETTINGS_SECTIONS", "PLACEHOLDER", "parse_rss_url",
    "section_key", "is_secret",
]

# 项目根：src/bt169/config.py → src/bt169 → src → 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "169bt.db"
IMAGE_DIR = DATA_DIR / "images"
UI_DIR = PROJECT_ROOT / "ui"

#: 落库前必须加密的设置键名（比对的是**最后一段**）。
SECRET_FIELDS = frozenset({"password", "token", "apikey", "api_key", "secret"})

#: 设置分区（与前端设置弹窗一一对应）。顺序即展示顺序。
SETTINGS_SECTIONS = ("site", "basic", "proxy", "emby", "tg")

#: 密钥字段读取时返回的占位符。提交时等于此值 → 视为「未修改」。
PLACEHOLDER = "\u2022" * 8  # ••••••••


class ConfigError(ValueError):
    """配置非法。"""


def section_key(section: str, key: str) -> str:
    """把分区内字段名转成存储键。

    ★ **必须有命名空间**：``settings`` 是扁平 KV 表，而 ``username`` /
    ``password`` 在「站点」与「网络代理」两个分区**都存在**。
    若直接存裸字段名，后写入的分区会静默覆盖先写入的——用户会发现
    「改了代理密码，论坛密码也没了」。
    """
    return f"{section}.{key}"


def is_secret(key: str) -> bool:
    """字段是否需加密。

    接受存储键（``site.password``）或裸字段名（``password``）——
    只比较**最后一段**，因此 ``panel_password`` 不会被误判为密钥字段。
    """
    return key.rsplit(".", 1)[-1] in SECRET_FIELDS


def parse_rss_url(raw: str) -> tuple[str, str]:
    """从 RSS 订阅链接解析出版块 fid，并返回清洗后的 URL。

    实测（REQUIREMENTS.md 事实 #21）：RSS 匿名完全可用，``auth`` 参数被服务端忽略。
    因此清洗时**只保留 fid**——不必要地在 URL 里携带账号凭据是安全风险。

    Args:
        raw: 用户填写的完整 RSS URL。

    Returns:
        ``(fid, clean_url)``。

    Raises:
        ConfigError: URL 为空、非法，或缺 ``fid`` 参数。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("RSS 订阅链接不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"RSS 订阅链接必须是完整的 http(s) URL：{raw!r}")

    fid = (parse_qs(parsed.query).get("fid") or [""])[0].strip()
    if not fid:
        raise ConfigError("RSS 订阅链接缺少 fid 参数（版块 ID）")
    if not fid.isdigit():
        raise ConfigError(f"fid 必须是数字：{fid!r}")

    clean = urlunparse(parsed._replace(query=f"fid={fid}", fragment=""))
    return fid, clean
