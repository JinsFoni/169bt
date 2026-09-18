"""运行状态：会话健康、采集状态、库内统计。

供前端轮询（`FRONTEND.md` §14.1 契约）：
- 展示「Cookie 剩 N 天」与自动续期结果
- 顶部采集按钮的进度
- 首次打开时判断是否需要提示登录
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from bt169 import __version__
from bt169.config import section_key
from bt169.repo.collect import CollectRepo, last_run_stats
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo
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
    settings = SettingsRepo(db, request.app.state.box)

    session = store.load()
    active_job = jobs.active()
    last_run, last_ok, failures = last_run_stats(jobs)
    counts = posts.count_by_status()

    return {
        "version": __version__,
        # ---- 会话（FRONTEND.md §14.1）
        "session": {
            "valid": bool(session and session.valid),
            "username": session.username if session else None,
            "expires_at": session.expires_at if session else None,
            "days_left": session.days_left if session else None,
            "needs_renewal": session.needs_renewal() if session else False,
            "renew_window_days": RENEW_BEFORE_DAYS,
            "last_relogin_at": session.last_relogin_at if session else None,
            "relogin_state": (session.relogin_state if session else None) or "ok",
            "login_attempts_left": session.login_attempts_left if session else None,
        },
        # ---- 采集器
        "collector": {
            "last_run_at": last_run,
            "last_ok_at": last_ok,
            "consecutive_failures": failures,
        },
        # ---- 正在跑的采集（前端顶部按钮进度条）
        "collect": {
            "running": active_job is not None,
            "job_id": active_job.id if active_job else None,
            "percent": active_job.percent if active_job else None,
            "phase": active_job.phase if active_job else None,
            "message": active_job.message if active_job else None,
        },
        # ---- 库内统计
        "library": {
            "total": sum(counts.values()),
            "by_status": counts,
        },
        # ---- Telegram（需求 T-4：未配置时前端禁用「下载」按钮）
        #
        # ★ 只回布尔值，**绝不回传 token/chat_id**——哪怕脱敏后的长度也不给。
        #   这个接口不经过设置面板的脱敏逻辑，得自己守住。
        "telegram": {
            "configured": _tg_configured(settings),
        },
        "last_error": None,
    }


def _tg_configured(settings: SettingsRepo) -> bool:
    """Bot Token 与 Chat ID 是否都已配置。

    两个都要有：只配了 Token 而没有 Chat ID 时发不出去，按钮该保持禁用。
    """
    token = (settings.get(section_key("tg", "token")) or "").strip()
    chat_id = (settings.get(section_key("tg", "chat_id")) or "").strip()
    return bool(token and chat_id)
