"""路径、常量与配置解析。"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = [
    "ConfigError", "PROJECT_ROOT", "DATA_DIR", "DB_PATH", "IMAGE_DIR", "UI_DIR",
    "SECRET_FIELDS", "SETTINGS_SECTIONS", "PLACEHOLDER", "parse_rss_url",
    "section_key", "is_secret",
    "FORUM_BASE", "DEFAULT_FID", "THREADS_PER_PAGE", "FETCH_DELAY_RANGE",
    "USER_AGENT", "MAX_BACKFILL_PAGES", "MAX_BACKFILL_DAYS",
    "AVDB_DEFAULT_BASE", "AVDB_LTX_WEBSITE", "AVDB_LTX_SECTION",
    "AVDB_PAGE_SIZE", "AVDB_MAX_PAGES", "AVDB_TIMEOUT", "LTX_SYNC_TTL",
]

# 项目根：src/bt169/config.py → src/bt169 → src → 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 数据目录。★ 可用 ``BT169_DATA_DIR`` 覆盖。
#:
#: 存在的理由：E2E 测试会通过真实 HTTP 写入**真实数据库**。没有这个开关时，
#: 测试脚本只能跑在生产库上——曾因此把用户的真实论坛账号密码覆盖成
#: ``e2e-user``（不可恢复）。有开关后测试跑在临时目录，怎么折腾都不伤真数据。
DATA_DIR = Path(
    os.environ.get("BT169_DATA_DIR") or (PROJECT_ROOT / "data")
)
DB_PATH = DATA_DIR / "169bt.db"
IMAGE_DIR = DATA_DIR / "images"

#: 前端静态资源目录。★ 可用 ``BT169_UI_DIR`` 覆盖。
#:
#: 存在的理由：Docker 镜像用 ``pip install .`` 装包，包里只有 ``bt169/``，
#: 不含仓库根的 ``ui/``。镜像里把 ``ui/`` 拷到 ``/app/ui`` 并设此变量指向它，
#: 服务即可正常出页面（默认值仍指向源码树，本地开发零配置）。~
UI_DIR = Path(
    os.environ.get("BT169_UI_DIR") or (PROJECT_ROOT / "ui")
)

#: 落库前必须加密的设置键名（比对的是**最后一段**）。
#:
#: ``jwt``：AVdb 登录换来的访问令牌（存为 ``ltx.jwt``）。它不面向用户，
#: 但持有它就等于持有那个 AVdb 账号——必须和密码同级加密，
#: 且 ``get_all(masked=True)`` 会把它换成占位符（不泄露给前端）。
SECRET_FIELDS = frozenset(
    {"password", "token", "apikey", "api_key", "secret", "api_hash", "session",
     "jwt"}
)

#: 设置分区（与前端设置弹窗一一对应）。顺序即展示顺序。
#:
#: ``ltx`` 紧跟 ``site``：两者都是「数据源」，在设置页里挨着才顺手。
SETTINGS_SECTIONS = ("site", "ltx", "basic", "proxy", "emby", "tg")

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
    因此清洗时**只保留 ``mod=rss`` 与 ``fid``**——不必要地在 URL 里携带
    账号凭据是安全风险。

    ★ **``mod=rss`` 绝不能丢**。这是本函数曾经的 bug：清洗后剩
    ``forum.php?fid=192``，看着像个正常的版块页，实测却返回 57 KB 的
    **版块 HTML**（0 个 ``<item>``）而不是 16 KB 的 RSS（20 个 ``<item>``）。
    轮询会把 HTML 当 feed 解析，静默拿到空列表，永远发现不了新帖。

    顺带把 Discuz 的 ``rss.php?fid=N`` 写法归一成 canonical 形式：
    实测 ``rss.php`` 会被 WAF 拦下返回 404。

    Args:
        raw: 用户填写的完整 RSS URL。

    Returns:
        ``(fid, clean_url)``。

    Raises:
        ConfigError: URL 为空、非法，缺 ``fid``，或 ``mod`` 不是 ``rss``。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("RSS 订阅链接不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"RSS 订阅链接必须是完整的 http(s) URL：{raw!r}")

    query = parse_qs(parsed.query)
    fid = (query.get("fid") or [""])[0].strip()
    if not fid:
        raise ConfigError("RSS 订阅链接缺少 fid 参数（版块 ID）")
    if not fid.isdigit():
        raise ConfigError(f"fid 必须是数字：{fid!r}")

    # mod 缺省时按「订阅链接」补齐；显式写成别的模块则拒绝——
    # 用户可能误贴了版块浏览页，静默改写会掩盖这个错误。
    mod = (query.get("mod") or ["rss"])[0].strip().lower()
    if mod != "rss":
        raise ConfigError(
            f"这不是 RSS 订阅链接（mod={mod!r}）；"
            "应形如 forum.php?mod=rss&fid=192"
        )

    # ★ 路径也归一到 forum.php：rss.php 会被 WAF 拦（实测 404）。
    clean = urlunparse(
        parsed._replace(path="/forum.php", query=f"mod=rss&fid={fid}", fragment="")
    )
    return fid, clean


# ---------------------------------------------------------------- 论坛抓取参数

#: 论坛根地址。实测可匿名访问（REQUIREMENTS.md 事实 #20）。
FORUM_BASE = "https://169bt.com"

#: 默认版块 fid（实测：目标版块为 192）。
DEFAULT_FID = "192"

#: 列表页每页帖子数（实测 28）。
THREADS_PER_PAGE = 28

#: 每页请求之间的最小/最大随机间隔（秒）。C-7 硬要求。
FETCH_DELAY_RANGE = (2.0, 5.0)

#: 浏览器 UA。论坛对默认 python-urllib UA 会拒绝。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

#: 回填时最多翻多少页，防止一次请求把论坛翻穿（28 × 200 ≈ 5600 帖）。
MAX_BACKFILL_PAGES = 200

#: 回填允许的最大天数跨度。
MAX_BACKFILL_DAYS = 366


# ---------------------------------------------------------------- AVdb（LTX 数据源）

#: AVdb 默认地址（用户自建实例，跑在同一台飞牛上）。
#:
#: ★ 存设置而不是写死：地址会变（端口、反代、换机器），而常量改动要发版。
#:   这里只作为「设置页的默认值」出现。
AVDB_DEFAULT_BASE = "http://192.168.0.108:8000"

#: LTX 板块在 AVdb 里的站点标识与版块名（实测：``website=ltx``，
#: ``section=LTX破解``，全部是 ed2k）。条数会涨：2026-10-28 实测 1450 条。
AVDB_LTX_WEBSITE = "ltx"
AVDB_LTX_SECTION = "LTX破解"

#: 单页条数。AVdb 的 ``ArticleQuery.page_size`` 上限是 100。
AVDB_PAGE_SIZE = 100

#: 翻页保护上限 —— 只在 ``hasMore`` 一直为真时兜底，**不是**条数上限。
#:
#: ★ 曾经写 20（配合当时的 658 条 / 7 页）。上游三周内涨到 1450 条（15 页），
#:   再涨到 2000 条就会**静默截断**：同步「成功」了，但后半段资源根本不在
#:   镜像里——用户只会发现某些日期的条目越来越少。翻页循环本身靠 ``hasMore``
#:   和空页退出，这个数只防「上游撒谎导致死循环」，给足余量更安全。
AVDB_MAX_PAGES = 100

#: 请求超时（秒）。AVdb 在局域网内，正常 0.4 s 一页。
AVDB_TIMEOUT = 30.0

#: LTX 镜像的「新鲜」时长（秒）。过期后前端打开 LTX 页会触发一次全量同步。
LTX_SYNC_TTL = 600
