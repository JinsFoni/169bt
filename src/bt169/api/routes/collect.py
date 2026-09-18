"""采集接口。

前端「手动采集」按钮 → ``POST /api/collect`` → 后台线程跑 → 轮询
``GET /api/collect/status``。

**所有采集接口都是同步返回的**：真正的抓取在后台线程里，
单帖 2–5 秒限速，一个几百帖的区间要跑几十分钟，绝不能让 HTTP 请求等着。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bt169 import config
from bt169.collector import CollectError, Collector, CollectRunner, ValidateError
from bt169.repo.collect import CollectJob, CollectRepo
from bt169.repo.posts import PostRepo
from bt169.source.forum import ForumClient
from bt169.source.session import SessionStore

router = APIRouter(prefix="/api/collect", tags=["collect"])


# ---------------------------------------------------------------- 依赖


def get_jobs(request: Request) -> CollectRepo:
    return CollectRepo(request.app.state.db)


def get_posts(request: Request) -> PostRepo:
    return PostRepo(request.app.state.db)


def get_runner(request: Request) -> CollectRunner:
    """取应用级单例 runner。

    ★ 必须存在 ``app.state`` 上而不是模块全局：runner 持有线程与锁，
    全局单例会让多个 app 实例（测试并行）互相干扰。
    """
    runner = getattr(request.app.state, "collect_runner", None)
    if runner is None:
        runner = _build_runner(request)
    return runner


def _build_runner(request: Request) -> CollectRunner:
    """按需装配 runner（惰性，避免 import 期副作用）。"""
    db = request.app.state.db
    sessions = SessionStore(db)

    # 复用已保存的会话 Cookie（匿名也能采集列表与大部分详情）
    session = sessions.load()
    cookies = session.cookies if session and session.valid else None

    client = ForumClient(cookies=cookies)
    collector = Collector(
        client=client,
        posts=PostRepo(db),
        jobs=CollectRepo(db),
    )
    runner = CollectRunner(collector, CollectRepo(db))
    request.app.state.collect_runner = runner
    return runner


# ---------------------------------------------------------------- 请求模型


class CollectRequest(BaseModel):
    """手动采集请求。"""

    from_date: str = Field(..., description="起始日期 YYYY-MM-DD（含）")
    to_date: str = Field(..., description="结束日期 YYYY-MM-DD（含）")
    fid: str = Field(default=config.DEFAULT_FID, description="版块 ID")


# ---------------------------------------------------------------- 序列化


def job_dto(job: CollectJob) -> dict[str, Any]:
    """任务的 API 表示。

    字段名用 ``snake_case`` 与其余 API 保持一致；前端只读不改。
    """
    return {
        "id": job.id,
        "from_date": job.from_date,
        "to_date": job.to_date,
        "fid": job.fid,
        "status": job.status,
        "phase": job.phase,
        "total": job.total,
        "processed": job.processed,
        "collected": job.collected,
        "skipped": job.skipped,
        "failed": job.failed,
        "pages": job.pages,
        "current_tid": job.current_tid,
        "message": job.message,
        "percent": job.percent,
        "cancel_requested": job.cancel_requested,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


# ---------------------------------------------------------------- 路由


@router.post("", status_code=202)
def start_collect(
    body: CollectRequest,
    runner: CollectRunner = Depends(get_runner),
) -> dict[str, Any]:
    """启动一次后台采集。

    立即返回（202），前端据 ``id`` 轮询 :func:`collect_status`。
    """
    try:
        job = runner.start(
            from_date=body.from_date, to_date=body.to_date, fid=body.fid
        )
    except ValidateError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_range", "message": str(exc)},
        ) from exc
    except CollectError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "collect_running", "message": str(exc)},
        ) from exc
    return {"job": job_dto(job)}


@router.get("/status")
def collect_status(
    jobs: CollectRepo = Depends(get_jobs),
) -> dict[str, Any]:
    """当前采集状态。

    无任务时 ``job`` 为 ``null``；有历史任务时附带 ``recent`` 供排查。
    """
    active = jobs.active()
    return {
        "job": job_dto(active) if active else None,
        "recent": [job_dto(j) for j in jobs.recent(5)],
    }


@router.get("/jobs/{job_id}")
def collect_job(
    job_id: int,
    jobs: CollectRepo = Depends(get_jobs),
) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"任务 {job_id} 不存在"},
        )
    return {"job": job_dto(job)}


@router.post("/jobs/{job_id}/cancel")
def cancel_collect(
    job_id: int,
    jobs: CollectRepo = Depends(get_jobs),
) -> dict[str, Any]:
    """请求取消。

    采集循环在每个帖子边界检查取消标志，因此**不是立即停止**——
    最坏情况要等当前帖的限速间隔走完（约 2–5 秒）。
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"任务 {job_id} 不存在"},
        )
    if not job.is_running:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_running",
                    "message": f"任务已处于 {job.status} 状态"},
        )
    jobs.request_cancel(job_id)
    updated = jobs.get(job_id)
    return {"job": job_dto(updated) if updated else None}
