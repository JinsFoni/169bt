"""采集任务仓储。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bt169.db import Database
from bt169.repo.posts import now_iso

__all__ = ["CollectJob", "CollectRepo", "JOB_RUNNING", "JOB_DONE",
           "JOB_FAILED", "JOB_CANCELLED", "TERMINAL_JOB_STATUSES",
           "last_run_stats"]

JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

TERMINAL_JOB_STATUSES = frozenset({JOB_DONE, JOB_FAILED, JOB_CANCELLED})


@dataclass(frozen=True, slots=True)
class CollectJob:
    """一次采集任务的快照。"""

    id: int
    from_date: str
    to_date: str
    fid: str
    status: str
    phase: str
    total: int
    processed: int
    collected: int
    skipped: int
    failed: int
    pages: int
    current_tid: int | None
    message: str | None
    cancel_requested: bool
    started_at: str
    finished_at: str | None

    @property
    def is_running(self) -> bool:
        return self.status == JOB_RUNNING

    @property
    def percent(self) -> int:
        """进度百分比（0-100）。``total`` 未知时返回 0。"""
        if self.total <= 0:
            return 100 if self.status in TERMINAL_JOB_STATUSES else 0
        return min(100, int(self.processed * 100 / self.total))


class CollectRepo:
    """``collect_jobs`` 表的读写。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------------------------------------------------------------- 查询

    def get(self, job_id: int) -> CollectJob | None:
        row = self._db.read().execute(
            "SELECT * FROM collect_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return _from_row(row) if row else None

    def active(self) -> CollectJob | None:
        """当前正在运行的任务（至多一个）。"""
        row = self._db.read().execute(
            "SELECT * FROM collect_jobs WHERE status=? ORDER BY id DESC LIMIT 1",
            (JOB_RUNNING,),
        ).fetchone()
        return _from_row(row) if row else None

    def recent(self, limit: int = 10) -> list[CollectJob]:
        rows = self._db.read().execute(
            "SELECT * FROM collect_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_from_row(r) for r in rows]

    # ---------------------------------------------------------------- 写入

    def create(self, *, from_date: str, to_date: str, fid: str) -> CollectJob:
        """新建一个 running 任务。

        Raises:
            RuntimeError: 已有任务在跑（由部分唯一索引兜底）。
        """
        try:
            with self._db.write() as conn:
                cur = conn.execute(
                    "INSERT INTO collect_jobs(from_date, to_date, fid, status,"
                    " phase, started_at) VALUES(?,?,?,?,?,?)",
                    (from_date, to_date, fid, JOB_RUNNING, "listing", now_iso()),
                )
                job_id = int(cur.lastrowid or 0)
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("已有采集任务正在运行") from exc
        job = self.get(job_id)
        assert job is not None
        return job

    def update(self, job_id: int, **fields: object) -> None:
        """更新任务的若干列。

        只接受白名单列名——避免把用户输入拼进 SQL。
        """
        allowed = {
            "status", "phase", "total", "processed", "collected", "skipped",
            "failed", "pages", "current_tid", "message", "cancel_requested",
            "finished_at",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"不允许更新的列：{sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._db.write() as conn:
            conn.execute(
                f"UPDATE collect_jobs SET {cols} WHERE id=?",
                (*fields.values(), job_id),
            )

    def bump(self, job_id: int, **deltas: int) -> None:
        """原子自增计数列（采集循环里每处理一帖调一次）。"""
        allowed = {"processed", "collected", "skipped", "failed", "pages"}
        bad = set(deltas) - allowed
        if bad:
            raise ValueError(f"不允许自增的列：{sorted(bad)}")
        if not deltas:
            return
        cols = ", ".join(f"{k}={k}+?" for k in deltas)
        with self._db.write() as conn:
            conn.execute(
                f"UPDATE collect_jobs SET {cols} WHERE id=?",
                (*deltas.values(), job_id),
            )

    def request_cancel(self, job_id: int) -> bool:
        """请求取消。返回是否确实改了状态。"""
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE collect_jobs SET cancel_requested=1 WHERE id=?"
                " AND status=?",
                (job_id, JOB_RUNNING),
            )
            return cur.rowcount > 0

    def is_cancel_requested(self, job_id: int) -> bool:
        row = self._db.read().execute(
            "SELECT cancel_requested FROM collect_jobs WHERE id=?", (job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])

    def finish(
        self, job_id: int, *, status: str, message: str | None = None
    ) -> None:
        """标记任务结束。"""
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"非终态：{status}")
        self.update(
            job_id, status=status, phase="done", message=message,
            current_tid=None, finished_at=now_iso(),
        )

    def reap_orphans(self) -> int:
        """把上次进程退出时遗留的 running 任务标记为失败。

        没有这一步，被 SIGKILL 掉的任务会永久占住「单任务」名额，
        用户再也无法启动新采集。
        """
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE collect_jobs SET status=?, phase='done',"
                " message=COALESCE(message, '进程中断'), finished_at=?"
                " WHERE status=?",
                (JOB_FAILED, now_iso(), JOB_RUNNING),
            )
            return cur.rowcount


def _from_row(row: sqlite3.Row) -> CollectJob:
    return CollectJob(
        id=row["id"],
        from_date=row["from_date"],
        to_date=row["to_date"],
        fid=row["fid"],
        status=row["status"],
        phase=row["phase"],
        total=row["total"],
        processed=row["processed"],
        collected=row["collected"],
        skipped=row["skipped"],
        failed=row["failed"],
        pages=row["pages"],
        current_tid=row["current_tid"],
        message=row["message"],
        cancel_requested=bool(row["cancel_requested"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def last_run_stats(repo: "CollectRepo") -> tuple[str | None, str | None, int]:
    """统计最近运行时间与连续失败数（供 ``/api/status`` 使用）。

    Returns:
        ``(last_run_at, last_ok_at, consecutive_failures)``
    """
    rows = repo._db.read().execute(
        "SELECT status, started_at, finished_at FROM collect_jobs"
        " ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        return None, None, 0

    last_run_at = rows[0]["finished_at"] or rows[0]["started_at"]
    last_ok_at = next(
        (r["finished_at"] or r["started_at"] for r in rows if r["status"] == JOB_DONE),
        None,
    )
    failures = 0
    for r in rows:
        if r["status"] in (JOB_FAILED, JOB_CANCELLED):
            failures += 1
        else:
            break
    return last_run_at, last_ok_at, failures
