"""归档日期与相邻导航。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from bt169.db import Database
from bt169.models import BROWSABLE_STATUSES

__all__ = ["DateCount", "ArchiveRepo"]

_STATUSES = tuple(sorted(BROWSABLE_STATUSES))
_PH = ", ".join("?" * len(_STATUSES))


@dataclass(frozen=True, slots=True)
class DateCount:
    date: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchiveRepo:
    """归档日期查询。只统计可浏览状态（``done``）的帖子。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_dates(self) -> list[DateCount]:
        """返回有帖的日期，**升序**（左旧右新）。"""
        rows = self._db.read().execute(
            f"SELECT post_date AS d, COUNT(*) AS c FROM posts"
            f" WHERE status IN ({_PH}) GROUP BY post_date ORDER BY post_date ASC",
            _STATUSES,
        ).fetchall()
        return [DateCount(date=r["d"], count=r["c"]) for r in rows]

    def latest_date(self) -> str | None:
        row = self._db.read().execute(
            f"SELECT MAX(post_date) AS d FROM posts WHERE status IN ({_PH})",
            _STATUSES,
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def count_for(self, date: str) -> int:
        row = self._db.read().execute(
            f"SELECT COUNT(*) AS c FROM posts"
            f" WHERE post_date=? AND status IN ({_PH})",
            (date, *_STATUSES),
        ).fetchone()
        return int(row["c"])

    def neighbour(self, date: str, direction: int) -> str | None:
        """相邻的**有帖**日期。

        用 ``>`` / ``<`` 而非日期算术——这样无帖的日期被自然跳过
        （实测 `data.js` 只有 5 个日期，中间存在空档）。

        Args:
            date: 当前日期 ``YYYY-MM-DD``。
            direction: ``-1`` 更早，``+1`` 更晚。

        Returns:
            相邻日期，或 ``None``（已在端点 / 日期不存在）。
        """
        if direction not in (-1, 1):
            raise ValueError(f"direction 必须是 -1 或 +1，收到 {direction}")

        op = "<" if direction < 0 else ">"
        order = "DESC" if direction < 0 else "ASC"
        row = self._db.read().execute(
            f"SELECT post_date AS d FROM posts"
            f" WHERE status IN ({_PH}) AND post_date {op} ?"
            f" GROUP BY post_date ORDER BY post_date {order} LIMIT 1",
            (*_STATUSES, date),
        ).fetchone()
        return row["d"] if row else None
