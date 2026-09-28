"""采集接口。

前端「手动采集」按钮 → ``POST /api/collect`` → 后台线程跑 → 轮询
``GET /api/collect/status``。

**所有采集接口都是同步返回的**：真正的抓取在后台线程里，
单帖 2–5 秒限速，一个几百帖的区间要跑几十分钟，绝不能让 HTTP 请求等着。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from bt169 import config
from bt169.collector import CollectError, Collector, CollectRunner, ValidateError
from bt169.collector.imagecache import ImageCache
from bt169.repo.collect import CollectJob, CollectRepo
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo
from bt169.source.forum import ForumClient
from bt169.source.session import SessionStore
from bt169.source.thanks import ThanksClient

log = logging.getLogger(__name__)

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

    ★ 与 RSS 轮询共用同一个实例（见 ``app._ensure_runner``）：
    各建一个就是各有一把锁、各有一套限速器。
    """
    runner = getattr(request.app.state, "collect_runner", None)
    if runner is None:
        runner = build_runner(request.app)
        request.app.state.collect_runner = runner
    return runner


def build_runner(app: Any) -> CollectRunner:
    """按需装配 runner（惰性，避免 import 期副作用）。

    ★ **登录是稀缺资源**（5 次 / 900 秒，按 IP），因此这里**不做**
    「启动时自动重登」。只做两件事：

    1. 复用已保存且仍有效的 Cookie；
    2. 若配置了站点账号密码，注入一个 :class:`ThanksClient`，
       让需要感谢的帖子在采集时就地解锁（C-4）。

    会话失效时的重登由 :class:`bt169.source.session` 的主动续期负责
    （见 ARCHITECTURE.md §5.2），不在这里抢额度。
    """
    runner = CollectRunner(build_collector(app), CollectRepo(app.state.db))
    _install_job_done_hook(runner._collector, app)
    return runner


def _install_job_done_hook(collector: Collector, app: Any) -> None:
    """把「采集完成 → 立即检查 Emby 入库状态」接到 collector 上。

    ★ **挂回调而不是在 collector 里同步 Emby**：采集器不该知道 Emby
    的存在（领域边界），装配层才知道「这台机器配了 Emby、用哪个库」。

    ★ **走 app 级 ``_sync_lock`` 而不是自建一把**：手动刷新、定时器
    （ ``EmbyScheduler.tick`` ）、采集回调三条路径可能同时到 Emby，
    三把锁等于没锁；共用一把才真的串行。

    ★ **只查本次采到的 tid**（``only_tids``）：刚采完的卡片用户正盯着，
    立即查才有意义；全库刚被定时器查过，陪跑是浪费。
    """
    from bt169.api.routes.emby import build_client, get_sync_lock
    from bt169.emby import EmbyNotConfigured, EmbySyncer
    from bt169.repo.posts import PostRepo
    from bt169.repo.settings import SettingsRepo

    def on_job_done(tids: list[int]) -> None:
        lock = get_sync_lock()
        if not lock.acquire(blocking=False):
            log.info("Emby 正在同步，跳过采集后的立即检查（%s 帖）", len(tids))
            return
        try:
            syncer = EmbySyncer(
                client=build_client(
                    SettingsRepo(app.state.db, app.state.box)),
                posts_repo=PostRepo(app.state.db),
                only_tids=tids,
            )
            result = syncer.sync()
            if result.error:
                log.warning("采集后 Emby 检查失败：" + result.error)
            elif result.checked:
                log.info(
                    "采集后 Emby 检查：检查 %s 帖，命中 %s",
                    result.checked, result.in_library,
                )
        except EmbyNotConfigured as exc:
            log.debug("采集后 Emby 检查跳过（未配置）：%s", exc)
        finally:
            lock.release()

    collector.on_job_done = on_job_done


def build_collector(app: Any) -> Collector:
    """构造采集器（手动采集与 RSS 轮询共用同一个）。

    ★ 共用意味着**共用一套限速器与一份 Cookie**。两套限速器会让
    「每帖 2–5 秒」形同虚设——那正是封号风险的来源。
    """
    db = app.state.db
    sessions = SessionStore(db)

    session = sessions.load()
    cookies = session.cookies if session and session.valid else None

    client = ForumClient(cookies=cookies, db=db)
    image_dir = getattr(app.state, "image_dir", None)
    return Collector(
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


def build_poller(app: Any) -> Any:
    """构造 RSS 轮询器（C-1）。每次调用重建——用户改配置后无需重启。

    返回 ``None`` 表示**未配置订阅链接**（不是错误，静默跳过）。
    """
    from bt169.collector import RssPoller

    db = app.state.db
    raw = SettingsRepo(db, app.state.box).get("site.rss_url")
    if not raw:
        return None
    _, clean = config.parse_rss_url(raw)     # 非法 URL 抛 ConfigError

    runner = getattr(app.state, "collect_runner", None)
    # ★ fallback 单建的 collector 也要挂钩子（与 build_runner 同源），
    #   否则 RSS 路径采完不触发 Emby 检查——两条路径必须行为一致。
    #   且只建一次：钩子必须装在真正传给 RssPoller 的那个实例上。
    if runner is not None:
        collector = runner._collector
    else:
        collector = build_collector(app)
        _install_job_done_hook(collector, app)
    return RssPoller(
        # ★ 复用 runner 的采集器：同一套限速器、同一份 Cookie。
        collector=collector,
        posts=PostRepo(db),
        jobs=CollectRepo(db),
        feed=build_feed_client(app),
        feed_url=clean,
        # ★ 走 runner 的锁：与手动采集真互斥（并发度恒为 1）。
        runner=runner,
    )


def build_feed_client(app: Any) -> ForumClient:
    """构造 RSS 拉取用的客户端。

    ★ **不带 Cookie**：RSS 匿名完全可用（事实 #21），把会话凭据发给
    一个不需要它的端点没有必要。这也让轮询在会话失效后仍能发现新帖。

    ★ 复用 ``ForumClient``：它已经处理好了 UA、超时与错误包装。
    """
    return ForumClient(db=app.state.db)


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


@router.post("/poll", status_code=202)
def poll_now(request: Request) -> dict[str, Any]:
    """立即跑一轮 RSS 轮询（C-1 的手动触发）。

    定时器每 5 分钟跑一次，这个接口给「刚发了新帖，现在就想要」的场景。

    ★ **先抢锁再起线程**：拿不到采集通道就**同步**返回 409。
    若先起线程再失败，前端会拿到 202 却什么都没发生，只能靠轮询
    状态猜——不如当场告诉它。

    ★ 抢到后把锁转交给后台线程，传 ``claim=False``：轮询器自己
    不必（也不该）再抢一次。
    """
    try:
        poller = build_poller(request.app)
    except config.ConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_rss_url", "message": str(exc)},
        ) from exc
    if poller is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_rss_url",
                    "message": "未配置 RSS 订阅链接，请在设置页填写"},
        )

    runner = get_runner(request)
    try:
        runner.start_poll(lambda claim: poller.tick(claim=claim))
    except CollectError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "collect_running", "message": str(exc)},
        ) from exc
    return {"accepted": True, "feed_url": poller.feed_url}


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
