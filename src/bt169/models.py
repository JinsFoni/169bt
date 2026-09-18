"""领域对象与 API DTO。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

__all__ = [
    "PostStatus", "VALID_STATUSES", "BROWSABLE_STATUSES", "SETTLED_STATUSES",
    "Post", "PostDTO",
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
            ed2k=post.ed2k,
            post_date=post.post_date,
            status=post.status,
            emby_in_library=emby_in_library,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
