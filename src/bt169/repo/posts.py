"""帖子读写。"""

from __future__ import annotations

from datetime import datetime

from bt169.db import Database
from bt169.models import BROWSABLE_STATUSES, VALID_STATUSES, Post

__all__ = ["PostRepo", "now_iso"]

_STATUSES = tuple(sorted(BROWSABLE_STATUSES))
_PH = ", ".join("?" * len(_STATUSES))


def now_iso() -> str:
    """当前时间，ISO 8601 带本地时区偏移。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PostRepo:
    """帖子查询与状态变更。

    浏览路径**只**返回 ``done`` 状态的帖子；其他状态是采集内部状态。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_by_date(self, date: str) -> list[Post]:
        """某归档日的帖子，**新帖在前**（``tid DESC``）。"""
        rows = self._db.read().execute(
            f"SELECT * FROM posts WHERE post_date=? AND status IN ({_PH})"
            f" ORDER BY tid DESC",
            (date, *_STATUSES),
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def get(self, tid: int) -> Post | None:
        row = self._db.read().execute(
            "SELECT * FROM posts WHERE tid=?", (tid,)
        ).fetchone()
        return Post.from_row(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        rows = self._db.read().execute(
            "SELECT status, COUNT(*) AS c FROM posts GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def delete(self, tid: int) -> bool:
        """**硬删除**一行（ADR-7）。

        图片删除不在这里做——`imagecache` 需要先做引用计数，
        见 `ARCHITECTURE.md` §5.8。

        Returns:
            True 表示删掉了行；False 表示本来就不存在（幂等）。
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM posts WHERE tid=?", (tid,))
            return cur.rowcount > 0

    def set_status(
        self,
        tid: int,
        status: str,
        *,
        error: str | None = None,
        next_retry_at: str | None = None,
    ) -> None:
        """更新状态机。

        转到 ``done`` 时**自动清空** ``last_error`` / ``next_retry_at``
        ——残留的错误信息会误导 `doctor` 与前端状态显示。
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"未知状态：{status!r}（合法值：{sorted(VALID_STATUSES)}）"
            )
        if status == "done":
            error = None
            next_retry_at = None

        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET status=?, last_error=?, next_retry_at=?,"
                " retry_count=retry_count+1, updated_at=? WHERE tid=?",
                (status, error, next_retry_at, now_iso(), tid),
            )

    def set_emby(
        self,
        tid: int,
        *,
        in_library: bool,
        item_id: str | None = None,
        checked_at: str,
    ) -> None:
        """记录 Emby 查询结果（缓存，避免每次浏览都打 Emby）。"""
        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET emby_status=?, emby_item_id=?, emby_checked=?,"
                " updated_at=? WHERE tid=?",
                (
                    "in_library" if in_library else "none",
                    item_id if in_library else None,
                    checked_at,
                    now_iso(),
                    tid,
                ),
            )

    def mark_tg_sent(self, tid: int, when: str) -> None:
        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET tg_sent_at=?, updated_at=? WHERE tid=?",
                (when, now_iso(), tid),
            )
