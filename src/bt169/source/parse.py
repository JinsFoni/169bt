"""论坛 HTML 解析。

所有解析都是**容错**的：字段缺失置空而非抛异常（风险表「字段格式变动」）。
DOM 定位用 ``selectolax``，正则只用于字段值提取。

★ 本模块**不发起网络请求**，因此可以完全离线测试。
这是整个项目最需要测试覆盖的地方。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from selectolax.parser import HTMLParser

__all__ = [
    "ListRow",
    "ThreadDetail",
    "parse_thread_list",
    "parse_thread_detail",
    "normalize_date",
    "extract_ed2k",
    "extract_codes",
    "is_login_required",
    "is_thanks_required",
]


# ------------------------------------------------------------------ 正则

#: 番号。与 `emby/matcher.py` 保持一致（实测 13/13）。
CODE_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9])([A-Z]{2,6}-\d{2,5})(?:[^0-9]|$)")

#: ed2k 链接。
ED2K_RE = re.compile(r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]+\|/")

#: 「作者」块中的绝对日期（Discuz 对老帖直接输出文本而非 title 属性）。
_ABS_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

#: `title="2026-9-14"` 形式的日期。
_TITLE_DATE_RE = re.compile(r'title="(\d{4})-(\d{1,2})-(\d{1,2})')

#: 中文标签提取。冒号全半角都接受，中间允许空白。
def _label_re(label: str) -> re.Pattern[str]:
    return re.compile(rf"\[?{re.escape(label)}\]?\s*[:：]\s*(.+?)(?:\s*<br|\s*<|\s*$)", re.S)


_RE_ACTRESS = re.compile(r"出演者\s*[:：]\s*([^<\r\n]+)")
_RE_RELEASE = re.compile(r"商品発売日\s*[:：]\s*(\d{4})/(\d{1,2})/(\d{1,2})")
_RE_SIZE = re.compile(r"\[影片大小\]\s*[:：]\s*([^<\r\n]+)")
_RE_TITLE_FIELD = re.compile(r"\[影片名称\]\s*[:：]\s*([^<\r\n]+)")

#: 帖子内嵌的封面/详情图（imgccc 图床）。
_IMGCCC_RE = re.compile(r"https?://[\w.-]*imgccc\.com/[^\s\"'<>]+")

#: 会话失效信号（ARCHITECTURE.md §5.1.1）。
_LOGIN_HINT = "您需要登录"

#: 快速回复框底部的固定文案。这些是**页面正常组成**，不是会话失效。
#: 实测每个页面（含未登录的列表页）都有。
_FOOTER_LOGIN_PHRASES = (
    "您需要登录后才可以发帖",
    "您需要登录才可以下载或查看",
    "您需要登录才能查看",
)

#: 真正的会话失效标志：登录表单被内嵌进当前页，或提示权限不足。
_LOGIN_WALL_MARKERS = (
    'id="loginform_',          # 内嵌登录表单
    "member.php?mod=logging&amp;action=login&amp;inajax",
    "抱歉，本帖要求阅读权限高于",
    "您无权进行当前操作",
)

#: 回复可见 / 感谢可见的隐藏内容标记。
_LOCKED_MARKERS = ('class="locked"', "如果您要查看本帖隐藏内容请")


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True, slots=True)
class ListRow:
    """列表页的一行。"""

    tid: int
    title: str
    post_date: str          # YYYY-MM-DD（UTC+8）
    reply_count: int | None


@dataclass(frozen=True, slots=True)
class ThreadDetail:
    """详情页解析结果。"""

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None    # 商品発売日
    size: str | None
    cover_img: str | None
    detail_img: str | None
    ed2k: str | None
    locked: bool                # True = 有隐藏内容且当前未解锁


# ------------------------------------------------------------------ 工具


def normalize_date(raw: str) -> str | None:
    """``2026-9-4`` → ``2026-09-04``；无法解析返回 None。

    实测论坛混用补零与不补零两种写法（page1 是 ``2026-9-14``，
    page2 起是 ``2026-09-10``），因此必须统一。
    """
    m = _ABS_DATE_RE.fullmatch(raw.strip())
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def extract_ed2k(text: str) -> str | None:
    """提取第一条 ed2k 链接。"""
    m = ED2K_RE.search(text)
    return m.group(0) if m else None


def extract_codes(text: str) -> set[str]:
    """提取所有「番号形状」的 token（大写）。

    用双向提取而非 ``in`` 判断，避免 ``START-62`` 命中 ``START-624``（E-5）。
    """
    return {m.group(1).upper() for m in CODE_RE.finditer(text)}


def is_login_required(html: str) -> bool:
    """详情页是否因会话失效而拿不到内容。

    ⚠️ **不能只搜「您需要登录」**：该串也出现在**每个页面底部的快速回复框**
    （「您需要登录后才可以发帖」）。实测列表页 page3 就因此被误判为会话失效。

    判定规则：出现真正的登录墙标志（内嵌登录表单 / 阅读权限提示），
    **并且**拿不到 ed2k。仅凭「您需要登录」四个字不算。
    """
    if extract_ed2k(html) is not None:
        return False

    # 先剥掉固定文案，剩下的才可能是真信号
    stripped = html
    for phrase in _FOOTER_LOGIN_PHRASES:
        stripped = stripped.replace(phrase, "")

    if _LOGIN_HINT in stripped and any(m in html for m in _LOGIN_WALL_MARKERS):
        return True
    return any(m in html for m in _LOGIN_WALL_MARKERS)


def is_thanks_required(html: str) -> bool:
    """是否需要「感谢」才能看到隐藏内容。"""
    return any(marker in html for marker in _LOCKED_MARKERS)


# ------------------------------------------------------------------ 列表页


def parse_thread_list(html: str) -> list[ListRow]:
    """解析版块列表页。

    实测结构（Discuz）：每行是 ``<tbody id="normalthread_{tid}">``。
    日期有两处，**必须取「作者」那一处**（发帖时间，归档依据），
    而不是「最后发表」（最后回复时间，会随回复变化）：

    - 新帖：``<span title="2026-9-14">4 天前</span>``
    - 老帖：``2026-9-10``（纯文本，无 title 属性）

    Args:
        html: 列表页 HTML。

    Returns:
        按页面顺序排列的行（论坛按发帖时间**降序**返回）。
    """
    tree = HTMLParser(html)
    rows: list[ListRow] = []

    for node in tree.css("tbody[id^='normalthread_']"):
        raw_id = node.attributes.get("id", "")
        try:
            tid = int(raw_id.removeprefix("normalthread_"))
        except ValueError:
            continue

        title_node = node.css_first("a.xst")
        title = title_node.text(strip=True) if title_node else ""

        # 「作者」块：list_author 之后、最后发表之前
        author_html = _author_block(node)
        post_date = _extract_author_date(author_html)
        if post_date is None:
            # 结构变动时不静默丢弃整行——tid 和标题仍然有价值
            post_date = ""

        reply_node = node.css_first("span.replynum")
        reply = None
        if reply_node is not None:
            digits = re.sub(r"\D", "", reply_node.text(strip=True))
            reply = int(digits) if digits else None

        rows.append(ListRow(tid=tid, title=title, post_date=post_date,
                            reply_count=reply))

    return rows


def _author_block(node) -> str:  # type: ignore[no-untyped-def]
    """取「作者」那一段 HTML（截断到「最后发表」之前）。"""
    try:
        html = node.html or ""
    except Exception:
        return ""
    seg = html.split("list_author", 1)[-1]
    return seg.split("最后发表")[0]


def _extract_author_date(author_html: str) -> str | None:
    """从作者块提取发帖日期。先试 ``title`` 属性，再试纯文本。"""
    m = _TITLE_DATE_RE.search(author_html)
    if m:
        return normalize_date("-".join(m.groups()))
    m = _ABS_DATE_RE.search(author_html)
    if m:
        return normalize_date("-".join(m.groups()))
    return None


# ------------------------------------------------------------------ 详情页


def parse_thread_detail(html: str, tid: int) -> ThreadDetail:
    """解析帖子详情页。

    字段全部容错：解析不到就是 ``None``，不抛异常。

    Args:
        html: 详情页 HTML。
        tid: 帖子 ID（页面里不一定可靠地出现，由调用方传入）。

    Returns:
        :class:`ThreadDetail`。
    """
    tree = HTMLParser(html)

    # 正文：Discuz 用 td.t_f 或 div.t_f
    body_node = tree.css_first("td.t_f") or tree.css_first("div.t_f")
    body = body_node.text(separator="\n", strip=True) if body_node else ""
    body_html = body_node.html if body_node else ""
    raw = html if not body else body

    # 标题：优先 h1，其次 title 标签
    title = ""
    h1 = tree.css_first("h1") or tree.css_first("#thread_subject")
    if h1:
        title = h1.text(strip=True)
    if not title:
        t = tree.css_first("title")
        title = t.text(strip=True) if t else ""

    ed2k = extract_ed2k(html)

    # 封面 / 详情图：正文里的 imgccc 链接，按出现顺序取前两张
    imgs = _unique(_IMGCCC_RE.findall(body_html or raw))

    actress = _first_group(_RE_ACTRESS, raw)
    release = _release_date(raw)
    size = _first_group(_RE_SIZE, raw)

    code: str | None = None
    field_title = _first_group(_RE_TITLE_FIELD, raw)
    for candidate in (field_title, title):
        codes = extract_codes(candidate or "")
        if codes:
            # 一帖通常只有一个番号；多个时取最长（更具体）
            code = sorted(codes, key=len, reverse=True)[0]
            break

    return ThreadDetail(
        tid=tid,
        title=title,
        code=code,
        actress=_clean(actress),
        release_date=release,
        size=_clean(size),
        cover_img=imgs[0] if imgs else None,
        detail_img=imgs[1] if len(imgs) > 1 else None,
        ed2k=ed2k,
        locked=ed2k is None and is_thanks_required(html),
    )


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _release_date(text: str) -> str | None:
    m = _RE_RELEASE.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _clean(value: str | None) -> str | None:
    """去掉 HTML 实体残留与首尾空白。"""
    if value is None:
        return None
    v = value.replace("&nbsp;", " ").replace("&amp;", "&").strip()
    v = re.sub(r"\s+", " ", v)
    return v or None
