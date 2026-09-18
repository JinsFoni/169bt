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

    def exists(self, tid: int) -> bool:
        """tid 是否已入库（**任意状态**）。

        采集的跳过规则是「数据库里有的就跳过」，与状态无关。
        用 ``SELECT 1 ... LIMIT 1`` 而非 ``COUNT(*)``：走主键索引即返回。
        """
        row = self._db.read().execute(
            "SELECT 1 FROM posts WHERE tid=? LIMIT 1", (tid,)
        ).fetchone()
        return row is not None

    def existing_tids(self, tids: list[int]) -> set[int]:
        """批量查询哪些 tid 已存在。

        采集时先用这个批量预筛，可以省掉逐条查询的往返。
        """
        if not tids:
            return set()
        out: set[int] = set()
        # SQLite 变量数上限 999（旧版）/ 32766（3.32+），分批保守处理
        for i in range(0, len(tids), 500):
            chunk = tids[i : i + 500]
            ph = ",".join("?" * len(chunk))
            rows = self._db.read().execute(
                f"SELECT tid FROM posts WHERE tid IN ({ph})", chunk
            ).fetchall()
            out.update(r["tid"] for r in rows)
        return out

    def upsert_collected(
        self,
        *,
        tid: int,
        title: str,
        code: str | None,
        actress: str | None,
        release_date: str | None,
        size: str | None,
        cover_img: str | None,
        detail_img: str | None,
        ed2k: str | None,
        post_date: str,
        status: str,
        cover_local: str | None = None,
        detail_local: str | None = None,
    ) -> None:
        """写入一条采集结果。

        ``ON CONFLICT`` 只在**同一 tid 被重新采集**时触发（例如上一轮
        ``failed`` 被用户重跑），此时**保留**已有的 emby/tg 派生状态——
        那些字段的更新由各自的流程负责，不该被采集覆盖成 NULL。

        ★ 本地图路径（``cover_local``/``detail_local``）用 ``COALESCE``
        保留旧值：图床可能临时抽风导致本轮没下到图，那时**不该**把上轮
        已下好的本地图路径抹掉（否则前端会从本地图退回外链）。
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态：{status}")
        now = now_iso()
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO posts(tid, title, code, actress, release_date, size,"
                " cover_img, detail_img, cover_local, detail_local, ed2k,"
                " post_date, status, retry_count, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)"
                " ON CONFLICT(tid) DO UPDATE SET"
                " title=excluded.title, code=excluded.code,"
                " actress=excluded.actress, release_date=excluded.release_date,"
                " size=excluded.size, cover_img=excluded.cover_img,"
                " detail_img=excluded.detail_img,"
                " cover_local=COALESCE(excluded.cover_local, posts.cover_local),"
                " detail_local=COALESCE(excluded.detail_local, posts.detail_local),"
                " ed2k=excluded.ed2k,"
                " post_date=excluded.post_date, status=excluded.status,"
                " last_error=NULL, next_retry_at=NULL, updated_at=excluded.updated_at",
                (
                    tid, title, code, actress, release_date, size, cover_img,
                    detail_img, cover_local, detail_local, ed2k, post_date,
                    status, now, now,
                ),
            )

    def references_image(self, url: str) -> bool:
        """是否还有别的帖子引用这张图（删除联动做引用计数）。

        依据**源 URL**：``ImageCache.key = sha1(源 URL)``，同 key 必然来自
        同 URL，所以按 URL 计数是精确的（不需要反解本地路径）。
        """
        row = self._db.read().execute(
            "SELECT COUNT(*) AS c FROM posts"
            " WHERE cover_img=? OR detail_img=?",
            (url, url),
        ).fetchone()
        return bool(row["c"])

    def missing_local_images(self) -> list[int]:
        """有图但未本地化的 tid（供 ``169bt doctor`` 统计覆盖率）。"""
        rows = self._db.read().execute(
            "SELECT tid FROM posts"
            " WHERE (cover_img IS NOT NULL AND cover_local IS NULL)"
            "    OR (detail_img IS NOT NULL AND detail_local IS NULL)"
        ).fetchall()
        return [r["tid"] for r in rows]

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
