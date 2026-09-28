"""运行状态：会话健康、采集状态、库内统计。

供前端轮询（`FRONTEND.md` §14.1 契约）：
- 展示「Cookie 剩 N 天」与自动续期结果
- 顶部采集按钮的进度
- 首次打开时判断是否需要提示登录
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

import httpx

from bt169 import __version__
from bt169.config import section_key
from bt169.repo.collect import CollectRepo, last_run_stats
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo
from bt169.source.session import RENEW_BEFORE_DAYS, SessionRenewer, SessionStore

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


def build_renewer(request: Request) -> SessionRenewer:
    """构造会话续期器（W-16）。

    ★ 凭据用**闭包延迟读取**：每次续期时才从库里取，用户改了密码不必重启。
    ★ 取凭据可能抛（主密钥坏了 / 解密失败）——``SessionRenewer`` 会吞掉
      并当作「无凭据」，不续期也不报错。
    """
    from bt169.source.captcha import PythonSolver
    from bt169.source.forum import ForumClient
    from bt169.source.login import LoginClient

    db = request.app.state.db
    settings = SettingsRepo(db, request.app.state.box)
    store = SessionStore(db)

    def credentials() -> tuple[str, str]:
        return (
            settings.get(section_key("site", "username")) or "",
            settings.get(section_key("site", "password")) or "",
        )

    class _ClientFactory:
        """每次登录新建一个 ForumClient，用完即关。

        ★ 不能常驻复用：登录是一次性动作，且旧连接会在 Cookie 失效后
          一直复用死连接。

        ★ 登录请求同样受应用内代理设置约束（采集链路的一部分）。
        """

        def login(self, username: str, password: str):  # type: ignore[no-untyped-def]
            from bt169.netproxy import httpx_kwargs

            client = ForumClient(
                client=httpx.Client(**httpx_kwargs(settings))
            )
            try:
                lc = LoginClient(client=client, store=store,
                                 solvers=[PythonSolver()])
                return lc.login(username, password)
            finally:
                client.close()

    return SessionRenewer(store=store, login_client=_ClientFactory(),
                          credentials=credentials)


@router.post("/status/renew")
def renew(
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    """手动触发一次会话续期（W-16）。

    ★ 未到续期窗口时**什么都不做**（``attempted: false``）——续期要消耗
      登录额度，不能因为用户点了按钮就无脑登一次。
    """
    return build_renewer(request).maybe_renew().to_dict()
