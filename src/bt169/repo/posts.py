"""帖子读写。"""

from __future__ import annotations

from datetime import datetime

from bt169.db import Database
from bt169.models import (
    BROWSABLE_STATUSES,
    SETTLED_STATUSES,
    VALID_STATUSES,
    Post,
)

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

    def all_posts(self) -> list[Post]:
        """全部帖子（不限状态、不限日期）。

        供 Emby 同步（E-1~E-7）使用：入库标记与帖子是否解锁无关，
        也不该只同步某一天。
        """
        rows = self._db.read().execute(
            "SELECT * FROM posts ORDER BY tid DESC"
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def list_by_tids(self, tids: list[int]) -> list[Post]:
        """按 tid 批量取帖，**保持传入顺序**，不存在的 tid 直接跳过。

        供「采集完成后立即检查入库状态」用：只查本次采到的帖子，
        而不是全库重跑一遍（万帖规模下全表重写是纯浪费）。

        SQLite 的 ``IN`` 占位符有数量上限（默认 999），分批查——
        每批 500，留足余量；一批最多也就 50 帖（日发帖峰值），
        这只是防御性上限，不是现实规模。
        """
        if not tids:
            return []
        by_tid: dict[int, Post] = {}
        CHUNK = 500
        for i in range(0, len(tids), CHUNK):
            chunk = tids[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._db.read().execute(
                f"SELECT * FROM posts WHERE tid IN ({ph})", chunk,
            ).fetchall()
            for r in rows:
                p = Post.from_row(r)
                by_tid[p.tid] = p
        return [by_tid[t] for t in tids if t in by_tid]

    def exists(self, tid: int) -> bool:
        """tid 是否已入库（**任意状态**）。

        用 ``SELECT 1 ... LIMIT 1`` 而非 ``COUNT(*)``：走主键索引即返回。

        ★ 注意：**跳过采集不该用这个**。它回答的是「库里有没有」，
        而采集要问的是「这个帖还有没有活要干」——见 :meth:`is_settled`。
        """
        row = self._db.read().execute(
            "SELECT 1 FROM posts WHERE tid=? LIMIT 1", (tid,)
        ).fetchone()
        return row is not None

    def is_settled(self, tid: int) -> bool:
        """tid 是否已**处理完毕**（终态，采集可以跳过）。

        终态 = ``done``（拿到 ed2k）或 ``nolink``（确认无链接）。
        其余状态（``pending`` / ``failed`` / ``thanked``）都是**未完成**，
        应该重试。

        ★ 为什么需要它：早先采集用 ``exists()`` 判断跳过，结果是
        「tid 在库里就跳过，不管状态」。这本身没错，但配上「会话失效时
        只标 pending」就成了一条死路——实测：13 帖在没会话时被采成
        ``pending``，用户配好凭据后重采，**一个都不会变**。而且前端只
        展示终态帖，``pending`` 在界面上看不见，连手动删掉重来都做不到。

        ★ 需求依据：C-2「已入库的跳过」说的是**省掉重复抓取**，
        C-8 则明确要求「失败可重试」。两者只能这样调和：
        有结论的跳过，没结论的重试。
        """
        row = self._db.read().execute(
            "SELECT status FROM posts WHERE tid=? LIMIT 1", (tid,)
        ).fetchone()
        return row is not None and row[0] in SETTLED_STATUSES

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
        cover_orig: str | None = None,
        detail_orig: str | None = None,
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
                " cover_img, detail_img, cover_local, detail_local,"
                " cover_orig, detail_orig, ed2k,"
                " post_date, status, retry_count, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)"
                " ON CONFLICT(tid) DO UPDATE SET"
                " title=excluded.title, code=excluded.code,"
                " actress=excluded.actress, release_date=excluded.release_date,"
                " size=excluded.size, cover_img=excluded.cover_img,"
                " detail_img=excluded.detail_img,"
                " cover_local=COALESCE(excluded.cover_local, posts.cover_local),"
                " detail_local=COALESCE(excluded.detail_local, posts.detail_local),"
                " cover_orig=COALESCE(excluded.cover_orig, posts.cover_orig),"
                " detail_orig=COALESCE(excluded.detail_orig, posts.detail_orig),"
                " ed2k=excluded.ed2k,"
                " post_date=excluded.post_date, status=excluded.status,"
                " last_error=NULL, next_retry_at=NULL, updated_at=excluded.updated_at",
                (
                    tid, title, code, actress, release_date, size, cover_img,
                    detail_img, cover_local, detail_local, cover_orig,
                    detail_orig, ed2k, post_date, status, now, now,
                ),
            )

    def set_local_image(self, tid: int, column: str, local_url: str) -> None:
        """只更新一个本地图路径（补图用，不动其余字段）。

        ``column`` 是**真实列名**（``cover_local`` / ``detail_local`` /
        ``cover_orig`` / ``detail_orig``），与 ``FIELD_TARGETS`` 里写的
        名字一致——两边同源才不会漂移。**白名单而非字符串拼接**：
        拼错列名会静默写错列。
        """
        col = {
            "cover_local": "cover_local",
            "detail_local": "detail_local",
            "cover_orig": "cover_orig",
            "detail_orig": "detail_orig",
        }.get(column)
        if col is None:
            raise ValueError(f"未知图片列：{column}")
        with self._db.write() as conn:
            conn.execute(
                f"UPDATE posts SET {col}=?, updated_at=? WHERE tid=?",
                (local_url, now_iso(), tid),
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
        """有图但未本地化的 tid（供 ``169bt doctor`` 统计覆盖率）。

        ★ 含原图档：灯箱要它才能显示原图，缺了就是「功能没完成」。
        """
        rows = self._db.read().execute(
            "SELECT tid FROM posts"
            " WHERE (cover_img IS NOT NULL AND cover_local IS NULL)"
            "    OR (detail_img IS NOT NULL AND detail_local IS NULL)"
            "    OR (cover_img IS NOT NULL AND cover_orig IS NULL)"
            "    OR (detail_img IS NOT NULL AND detail_orig IS NULL)"
        ).fetchall()
        return [r["tid"] for r in rows]

    def missing_original_images(self) -> list[int]:
        """缺**原图档**的 tid（供 ``169bt doctor`` 提示重跑采集）。

        ★ 单独一个方法而不是并进 ``missing_local_images``：两者的
        严重程度不同。缩略图缺失 → 前端裂图（必须修）；原图档缺失 →
        灯箱显示得糊一些（可以慢慢补，老帖子本来就还没采过）。
        混在一起会让 doctor 天天报一堆「失败」。
        """
        rows = self._db.read().execute(
            "SELECT tid FROM posts"
            " WHERE (cover_img IS NOT NULL AND cover_orig IS NULL)"
            "    OR (detail_img IS NOT NULL AND detail_orig IS NULL)"
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
        self.set_emby_many(
            [(tid, in_library, item_id)], checked_at=checked_at)

    def set_emby_many(
        self,
        results: list[tuple[int, bool, str | None]],
        *,
        checked_at: str,
    ) -> None:
        """批量记录 Emby 查询结果。

        ``results`` 是 ``(tid, in_library, item_id)`` 三元组。与逐条
        ``set_emby`` 的区别**不只是省 SQL**：写走单例连接 + 全局锁
        （见 ``db.write``），万帖全量同步逐条写 = 万次锁往返；
        一把事务写完，无论 5 帖还是 1 万帖都是一次提交。

        **必须单事务**：检查时刻 ``checked_at`` 是「本次媒体库快照的
        生成时间」，分多次提交的话，读到一半的观测者会看到同一快照
        里的帖子带着不同的检查时间。
        """
        if not results:
            return
        ts = now_iso()
        with self._db.write() as conn:
            conn.executemany(
                "UPDATE posts SET emby_status=?, emby_item_id=?, emby_checked=?,"
                " updated_at=? WHERE tid=?",
                [
                    (
                        "in_library" if in_lib else "none",
                        item_id if in_lib else None,
                        checked_at,
                        ts,
                        tid,
                    )
                    for tid, in_lib, item_id in results
                ],
            )

    def mark_tg_sent(self, tid: int, when: str) -> None:
        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET tg_sent_at=?, updated_at=? WHERE tid=?",
                (when, now_iso(), tid),
            )
