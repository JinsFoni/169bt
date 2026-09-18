"""运行状态：会话健康、采集状态、库内统计。

供前端轮询，用于：
- 显示「会话还有 N 天过期」与自动续期结果（FRONTEND.md §5.2）
- 顶部采集按钮的进度显示
- 首次打开时判断是否需要提示登录
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from bt169 import __version__
from bt169.repo.collect import CollectRepo
from bt169.repo.posts import PostRepo
from bt169.source.session import RENEW_BEFORE_DAYS, SessionStore

router = APIRouter(prefix="/api", tags=["status"])


def get_session_store(request: Request) -> SessionStore:
    return SessionStore(request.app.state.db)


@router.get("/status")
def status(
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    """汇总运行状态。

    ⚠️ **绝不返回 Cookie 内容**——只返回派生信息（是否有效、剩余天数）。
    """
    db = request.app.state.db
    posts = PostRepo(db)
    jobs = CollectRepo(db)

    session = store.load()
    active_job = jobs.active()

    counts = posts.count_by_status()

    return {
        "version": __version__,
        "session": {
            "logged_in": bool(session and session.valid),
            "username": session.username if session else None,
            "days_left": session.days_left if session else None,
            "needs_renewal": session.needs_renewal() if session else False,
            "renew_window_days": RENEW_BEFORE_DAYS,
            "attempts_left": session.login_attempts_left if session else None,
            "state": session.relogin_state if session else None,
            "last_relogin_at": session.last_relogin_at if session else None,
        },
        "collect": {
            "running": active_job is not None,
            "job_id": active_job.id if active_job else None,
            "percent": active_job.percent if active_job else None,
        },
        "library": {
            "total": sum(counts.values()),
            "by_status": counts,
        },
    }
