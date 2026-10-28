"""LTX 数据源的读写（AVdb 镜像表 ``ltx_items``）。

与 :mod:`bt169.repo.posts` 的关系：**并行**，不是扩展。4K 与 LTX 是两个
独立来源（不同站点、不同主键语义、不同采集方式），各自一张表；界面在
顶栏切换，DTO 形状对齐（见 :class:`bt169.models.LtxPostDTO`），
前端渲染代码因此可以完全复用。
"""

from __future__ import annotations

from typing import Any

from bt169.db import Database
from bt169.models import LtxItem

__all__ = ["LtxRepo"]

#: ``IN (...)`` 占位符上限保护（SQLite 默认 999）。658 条正常规模，
#: 但同步可能涨到上千，分批写。
_CHUNK = 400

_UPSERT = """
INSERT INTO ltx_items (
    id, number, title, section, publish_date, size_bytes, ed2k,
    cover_url, detail_url, rejected, created_at, tg_sent_at, fetched_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)
ON CONFLICT(id) DO UPDATE SET
    number       = excluded.number,
    title        = excluded.title,
    section      = excluded.section,
    publish_date = excluded.publish_date,
    size_bytes   = excluded.size_bytes,
    ed2k         = excluded.ed2k,
    cover_url    = excluded.cover_url,
    detail_url   = excluded.detail_url,
    rejected     = excluded.rejected,
    created_at   = excluded.created_at,
    fetched_at   = excluded.fetched_at
"""


class LtxRepo:
    """LTX 镜像表：同步写入 + 按日期浏览 + 已转发标记。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------ 同步写入

    def upsert_many(self, rows: list[dict[str, Any]], fetched_at: str) -> int:
        """整批插入/更新，返回处理行数。

        ★ ``tg_sent_at`` **不在 UPDATE 列表里**：那是本地状态（我们有没有
          把这条 ed2k 转发给 Telegram），AVdb 不知道也不该被同步覆盖——
          真被覆盖的话每次同步后所有「已下载」标记都会消失，
          用户会重复转发。
        """
        if not rows:
            return 0
        values = [
            (
                r["id"], r.get("number"), r.get("title") or "",
                r.get("section"), r["publish_date"], r.get("size_bytes"),
                r.get("ed2k"), r.get("cover_url"), r.get("detail_url"),
                1 if r.get("rejected") else 0, r.get("created_at"), fetched_at,
            )
            for r in rows
        ]
        with self._db.write() as conn:
            conn.executemany(_UPSERT, values)
        return len(values)

    def prune(self, keep_ids: list[int]) -> int:
        """删除本次同步里已不存在的行（AVdb 侧被删/被清空），返回删除数。

        ★ 只在**拿到非空结果**时调用（见 api/routes/ltx.py）：上游临时返回
          空列表时若照删不误，用户会看到整站资源凭空消失，下次同步又回来。
        """
        if not keep_ids:
            return 0
        keep = set(keep_ids)
        rows = self._db.read().execute("SELECT id FROM ltx_items").fetchall()
        gone = [r["id"] for r in rows if r["id"] not in keep]
        if not gone:
            return 0
        with self._db.write() as conn:
            for i in range(0, len(gone), _CHUNK):
                chunk = gone[i:i + _CHUNK]
                ph = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM ltx_items WHERE id IN ({ph})", chunk)
        return len(gone)

    # ------------------------------------------------------------ 浏览查询

    def list_dates(self) -> list[tuple[str, int]]:
        """有资源的日期与条数，**升序**（左旧右新，与 /api/dates 同约定）。"""
        rows = self._db.read().execute(
            "SELECT publish_date AS d, COUNT(*) AS c FROM ltx_items"
            " WHERE rejected=0 GROUP BY publish_date ORDER BY publish_date ASC"
        ).fetchall()
        return [(r["d"], r["c"]) for r in rows]

    def list_by_date(self, date: str) -> list[LtxItem]:
        """某日资源，**新的在前**。

        排序用 ``created_at``（AVdb 的导出时间，同日内的真实先后），
        缺失时退到 ``id DESC``——id 是 AVdb 的合成雪花号，同样单调。
        """
        rows = self._db.read().execute(
            "SELECT * FROM ltx_items WHERE publish_date=? AND rejected=0"
            " ORDER BY COALESCE(created_at, '') DESC, id DESC",
            (date,),
        ).fetchall()
        return [LtxItem.from_row(r) for r in rows]

    def get(self, item_id: int) -> LtxItem | None:
        row = self._db.read().execute(
            "SELECT * FROM ltx_items WHERE id=?", (item_id,)
        ).fetchone()
        return LtxItem.from_row(row) if row else None

    def count(self) -> int:
        row = self._db.read().execute(
            "SELECT COUNT(*) AS c FROM ltx_items WHERE rejected=0"
        ).fetchone()
        return int(row["c"]) if row else 0

    def sent_tids(self, ids: list[int]) -> set[int]:
        """这批 id 里**已转发过**的集合（批量转发前预筛用，可选优化）。"""
        if not ids:
            return set()
        out: set[int] = set()
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._db.read().execute(
                f"SELECT id FROM ltx_items WHERE tg_sent_at IS NOT NULL"
                f" AND id IN ({ph})", chunk,
            ).fetchall()
            out.update(r["id"] for r in rows)
        return out

    # ------------------------------------------------------------ 状态

    def mark_tg_sent(self, item_id: int, when: str) -> None:
        with self._db.write() as conn:
            conn.execute(
                "UPDATE ltx_items SET tg_sent_at=? WHERE id=?", (when, item_id)
            )

    def touch_cover(self, item_id: int, cover_url: str) -> None:
        """记住解析后的封面源 URL（AVdb 有时只在同步时给一次）。"""
        with self._db.write() as conn:
            conn.execute(
                "UPDATE ltx_items SET cover_url=? WHERE id=? AND (cover_url IS NULL"
                " OR cover_url='')",
                (cover_url, item_id),
            )
