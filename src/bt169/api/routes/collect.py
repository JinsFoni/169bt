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
from bt169.collector.imagecache import ImageCache
from bt169.repo.collect import CollectJob, CollectRepo
from bt169.repo.posts import PostRepo
from bt169.source.forum import ForumClient
from bt169.source.session import SessionStore
from bt169.source.thanks import ThanksClient

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
    """按需装配 runner（惰性，避免 import 期副作用）。

    ★ **登录是稀缺资源**（5 次 / 900 秒，按 IP），因此这里**不做**
    「启动时自动重登」。只做两件事：

    1. 复用已保存且仍有效的 Cookie；
    2. 若配置了站点账号密码，注入一个 :class:`ThanksClient`，
       让需要感谢的帖子在采集时就地解锁（C-4）。

    会话失效时的重登由 :class:`bt169.source.session` 的主动续期负责
    （见 ARCHITECTURE.md §5.2），不在这里抢额度。
    """
    db = request.app.state.db
    settings = SettingsRepo(db, request.app.state.box)
    sessions = SessionStore(db)

    session = sessions.load()
    cookies = session.cookies if session and session.valid else None

    client = ForumClient(cookies=cookies)
    image_dir = getattr(request.app.state, "image_dir", None)
    collector = Collector(
        client=client,
        posts=PostRepo(db),
        jobs=CollectRepo(db),
        # ★ 只有拿着有效会话才注入感谢：匿名会话下感谢必然失败，
        #   不注入就自然停在 pending，不会白跑一轮限速请求。
        thanks=ThanksClient(client=client) if cookies else None,
        # ★ 图片本地化（W-15）。采集是串行限速的，转码 ~100 ms 可忽略；
        #   预生成后 /img 直接静态分发，零开销。
        images=ImageCache(image_dir) if image_dir else None,
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
