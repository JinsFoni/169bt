"""领域对象与 API DTO。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

__all__ = [
    "PostStatus", "VALID_STATUSES", "BROWSABLE_STATUSES", "SETTLED_STATUSES",
    "Post", "PostDTO", "LtxItem", "LtxPostDTO", "format_size",
]

PostStatus = Literal["pending", "thanked", "done", "failed", "nolink"]

#: 全部合法状态。
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "thanked", "done", "failed", "nolink"}
)

#: 浏览视图只展示这些状态。
BROWSABLE_STATUSES: frozenset[str] = frozenset({"done"})

#: **终态**——已有结论，采集可以跳过。
#:
#: - ``done``：拿到 ed2k
#: - ``nolink``：确认无链接（感谢了也没链接，或本来就不需要）
#:
#: 其余状态（``pending`` / ``thanked`` / ``failed``）都是**未完成**：
#: 要么等解锁，要么等重试。★ 采集跳过只能用这个集合，不能用
#: 「在不在库里」——否则未完成的帖会永久卡死（实测踩过，见
#: ``PostRepo.is_settled`` 的说明）。
SETTLED_STATUSES: frozenset[str] = frozenset({"done", "nolink"})


@dataclass(frozen=True, slots=True)
class Post:
    """领域对象。字段名与 ``posts`` 表列名**一一对应**。"""

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover_img: str | None
    detail_img: str | None
    cover_local: str | None
    detail_local: str | None
    cover_orig: str | None
    detail_orig: str | None
    ed2k: str | None
    post_date: str
    post_time: str | None
    status: str
    retry_count: int
    last_error: str | None
    next_retry_at: str | None
    emby_status: str | None
    emby_item_id: str | None
    emby_checked: str | None
    tg_sent_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Post:
        """从 ``sqlite3.Row`` 构造。

        按字段名逐个取值（而非 ``**row``），这样列名漂移会立刻
        ``KeyError`` 而不是静默丢数据。
        """
        return cls(**{f.name: row[f.name] for f in fields(cls)})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PostDTO:
    """API 输出对象。

    字段名对齐前端 ``app.js`` 的既有消费方式（ADR-11）：
    ``cover_img`` → ``cover``，``detail_img`` → ``detail``。
    内部字段（重试、错误、时间戳）**不外泄**。
    """

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover: str | None
    detail: str | None
    ed2k: str | None
    post_date: str
    status: str
    emby_in_library: bool
    #: TG 转发时间（T-7）。★ 前端用它把按钮显示为「已发」——
    #: 不返回的话用户看不出哪条已经转发过，会重复点。
    tg_sent_at: str | None = None
    #: ★ 灯箱用**原图**（点卡片后的大图）。
    #:
    #: ``cover``/``detail`` 是给**卡片缩略图**用的 600px 档；灯箱把图按
    #: ~1129 CSS px 渲染，用 600px 那档等于放大 1.88×（DPR=2 时 3.8×）——
    #: 实测这就是大图模糊的原因。
    #:
    #: 回退顺序：本地原图 → 本地缩略图 → 图床源 URL。
    #: 回退而非置空：老帖子（迁移前采集的）还没有原图档，
    #: 灯箱得能照常显示，只是清晰度差些。
    cover_orig: str | None = None
    detail_orig: str | None = None

    @classmethod
    def from_post(cls, post: Post, *, emby_in_library: bool = False) -> PostDTO:
        """构造 DTO。

        ``cover``/``detail`` **优先给本地化路径**（W-15），本地没有时
        回退到图床源 URL。这样：

        - 前端无需任何改动（字段名与语义不变，ADR-11/ADR-16）
        - 图床挂掉不影响已本地化的图
        - 尚未本地化（或本地化失败）的帖仍能显示，只是走外链
        """
        return cls(
            tid=post.tid,
            title=post.title,
            code=post.code,
            actress=post.actress,
            release_date=post.release_date,
            size=post.size,
            cover=post.cover_local or post.cover_img,
            detail=post.detail_local or post.detail_img,
            cover_orig=post.cover_orig or post.cover_local or post.cover_img,
            detail_orig=post.detail_orig or post.detail_local or post.detail_img,
            ed2k=post.ed2k,
            post_date=post.post_date,
            status=post.status,
            emby_in_library=emby_in_library,
            tg_sent_at=post.tg_sent_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- LTX 数据源

#: 灯箱档宽度（与 ``collector/imagecache.LIGHTBOX_WIDTH`` 保持一致）。
#:
#: ★ 这里不 import imagecache：那个模块为了转码 import 了 PIL，而 models 是
#:   领域层，被几乎所有测试直接 import。两个整数不值得把 PIL 拉成硬依赖。
#:   ``tests/test_ltx_dto.py`` 有一条断言钉住两者相等。
_LIGHTBOX_WIDTH = 1200


def format_size(size_bytes: int | None) -> str | None:
    """字节数 → 卡片显示用的体积（``3607826070`` → ``"3.4GB"``）。

    写法对齐 4K 帖子里抓到的原文风格（``7GB`` / ``1.2GB``）：无空格、
    一位小数、单位大写。**与 ``posts.size`` 同格式**，卡片因此不需要
    为两个来源写两套渲染。
    """
    if not size_bytes or size_bytes <= 0:
        return None
    gb = size_bytes / 1024 ** 3
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = size_bytes / 1024 ** 2
    return f"{mb:.0f}MB"


@dataclass(frozen=True, slots=True)
class LtxItem:
    """``ltx_items`` 表的一行（AVdb LTX 破解板块的本地镜像）。

    ★ ``id`` 是 **AVdb 的资源 tid**（16 位合成号），与论坛 tid 无关；
      DTO 里它顶上 ``tid`` 的位置，前端「按 tid 找卡」的既有逻辑照旧可用。
    """

    id: int
    number: str | None
    title: str
    section: str | None
    publish_date: str
    size_bytes: int | None
    ed2k: str | None
    cover_url: str | None
    detail_url: str | None
    rejected: int
    created_at: str | None
    tg_sent_at: str | None
    fetched_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LtxItem:
        return cls(**{f.name: row[f.name] for f in fields(cls)})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LtxPostDTO:
    """API 输出对象：**字段名与 :class:`PostDTO` 完全一致**。

    ★ 为什么强行对齐：前端 ``cardHTML`` / 灯箱 / 复制 / 转发都按
      ``{tid, title, code, cover, cover_orig, ed2k, size, release_date,
      tg_sent_at}`` 消费。对齐之后 LTX 页复用同一套渲染代码，
      而不是复制一份改字段名——两份卡片模板以后必然走形。

    ``actress`` 恒为 ``None``（AVdb 的 LTX 条目没有演员字段）；
    ``detail`` 同理（只有一张封面）。
    """

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover: str | None
    detail: str | None
    ed2k: str | None
    post_date: str
    status: str
    emby_in_library: bool
    tg_sent_at: str | None = None
    cover_orig: str | None = None
    detail_orig: str | None = None

    @classmethod
    def from_item(
        cls,
        item: LtxItem,
        *,
        cover: str | None = None,
        cover_orig: str | None = None,
    ) -> LtxPostDTO:
        """``cover``/``cover_orig`` 可由路由层覆盖；缺省走「封面代理端点」。

        缺省值是 ``/api/ltx/cover/{id}``——它按需下载、转码、落盘再回字节。
        这样**列表接口不逐张下载图片**（20 张卡串行下载会把首屏拖到十几秒），
        图片由浏览器并行请求、逐张本地化。

        ★ 灯箱要 1200px 档，所以 ``cover_orig`` 多带一个 ``?w=``：卡片用
          600（默认），灯箱用 1200。宽度常量抄自
          ``collector/imagecache.py``（models 不 import 它，避免为了两个
          整数把 PIL 拖进领域层），一致性由 ``tests/test_ltx_dto.py`` 钉住。
        """
        base = f"/api/ltx/cover/{item.id}" if item.cover_url else None
        fallback = base
        fallback_orig = (
            f"{base}?w={_LIGHTBOX_WIDTH}" if base else None
        )
        return cls(
            tid=item.id,
            title=item.title or "",
            code=item.number,
            actress=None,
            release_date=item.publish_date,
            size=format_size(item.size_bytes),
            cover=cover or fallback,
            detail=None,
            cover_orig=cover_orig or fallback_orig,
            detail_orig=None,
            ed2k=item.ed2k,
            post_date=item.publish_date,
            status="done",
            emby_in_library=False,
            tg_sent_at=item.tg_sent_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
