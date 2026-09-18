"""领域对象与 API DTO。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

__all__ = [
    "PostStatus", "VALID_STATUSES", "BROWSABLE_STATUSES", "Post", "PostDTO",
]

PostStatus = Literal["pending", "thanked", "done", "failed", "nolink"]

#: 全部合法状态。
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "thanked", "done", "failed", "nolink"}
)

#: 浏览视图只展示这些状态。
BROWSABLE_STATUSES: frozenset[str] = frozenset({"done"})


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
        return cls(
            tid=post.tid,
            title=post.title,
            code=post.code,
            actress=post.actress,
            release_date=post.release_date,
            size=post.size,
            cover=post.cover_img,
            detail=post.detail_img,
            ed2k=post.ed2k,
            post_date=post.post_date,
            status=post.status,
            emby_in_library=emby_in_library,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
