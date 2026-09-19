"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from bt169 import __version__
from bt169.config import IMAGE_DIR, UI_DIR, section_key
from bt169.emby import SYNC_INTERVAL_SECONDS
from bt169.crypto import SecretBox
from bt169.db import Database
from bt169.source.parse import TZ_ARCHIVE

__all__ = ["create_app"]

log = logging.getLogger(__name__)


def create_app(
    db: Database,
    *,
    box: SecretBox,
    ui_dir: Path | None = UI_DIR,
    image_dir: Path | None = IMAGE_DIR,
    emby_interval: float = 0,
) -> FastAPI:
    """装配应用。

    Args:
        db: 已迁移的数据库。
        box: 设置密钥字段的加解密器。
        ui_dir: 前端目录；``None`` 表示不挂载静态资源（API 单测用）。
        image_dir: 本地化图片目录（W-15）；``None`` 表示不挂载 ``/img``。
        emby_interval: Emby 定时同步间隔（秒）。
            ★ **默认 0 = 不启定时器**：每个测试都建 app，默认开启会
            给每个测试起一个后台线程（且一启动就跑一次同步），既慢又容易
            变成不可复现的闪烁。生产入口 ``__main__.serve`` 显式传
            ``None`` 启用（见 E-6）。
    """
    app = FastAPI(
        title="169bt 归档台",
        version=__version__,
        docs_url=None,          # 个人自用，不需要 Swagger UI 暴露面
        redoc_url=None,
        openapi_url=None,
        lifespan=_make_lifespan(emby_interval),
    )
    app.state.db = db
    app.state.box = box
    app.state.image_dir = image_dir

    _install_error_handler(app)
    _install_emby_scheduler(app, emby_interval)
    _install_poll_scheduler(app, emby_interval)
    from bt169.api.gate import GateMiddleware
    from bt169.api.routes import (
        auth,
        collect,
        emby,
        health,
        posts,
        settings,
        status,
        telegram,
    )

    # 门禁中间件包裹整个应用（与路由注册顺序无关）；放在 include_router
    # 之前只是为了阅读顺序：先立边界，再挂内容。
    app.add_middleware(GateMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(posts.router)
    app.include_router(settings.router)
    app.include_router(collect.router)
    app.include_router(status.router)
    app.include_router(telegram.router)
    app.include_router(emby.router)

    # 静态资源必须**最后**挂载：Starlette 按注册顺序匹配，
    # 挂在 "/" 的 StaticFiles 会吞掉之后注册的所有路由。
    #
    # ``/img`` 是本地化图片（W-15）。文件名含内容哈希（``sha1(源 URL)``
    # + 宽度），换图必然换文件名 → 可以永久缓存。
    #
    # ★ 目录必须先建出来：``StaticFiles(check_dir=True)`` 在目录不存在时
    #   直接抛错，而 ``ImageCache`` 是**首次下载时**才懒建子目录的。
    #   若在这里因目录不存在而跳过挂载，全新安装的 ``/img`` 会 404 直到
    #   重启——已入库的本地图全部显示不出来。
    if image_dir is not None:
        image_dir = Path(image_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/img",
            _ImmutableStaticFiles(directory=str(image_dir)),
            name="images",
        )
    if ui_dir is not None and Path(ui_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


def _make_lifespan(interval: float):
    """构造 lifespan：启动/停止三个后台定时器。

    三个定时器共用一个开关（``emby_interval``）：

    - Emby 入库同步（E-6）
    - 会话续期（W-16）
    - RSS 轮询发现新帖（C-1）

    ★ 用 ``lifespan`` 而非已弃用的 ``on_event``：后者会在每次注册时
    追加处理器，且 FastAPI 已标记弃用。

    ★ 间隔为 0 时返回一个什么都不做的 lifespan——测试入口就是这种。
    每个测试都建 app，默认开定时器会给每个测试起三个线程。
    """
    if not interval:
        return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = _build_emby_scheduler(app, interval)
        app.state.emby_scheduler = scheduler
        scheduler.start()

        # W-16：会话续期。与 Emby 用同一个开关（测试传 0 则都不启）。
        renewer = _build_renew_loop(app, RENEW_CHECK_SECONDS)
        app.state.renew_scheduler = renewer
        renewer.start()

        # C-1：RSS 轮询。
        poller = _build_poll_loop(app, POLL_CHECK_SECONDS)
        app.state.poll_scheduler = poller
        poller.start()
        try:
            yield
        finally:
            poller.stop()
            renewer.stop()
            scheduler.stop()

    return lifespan


def _build_emby_scheduler(app: FastAPI, interval: float):
    """构造定时同步器。每次 tick 重建同步器——用户在设置页改地址后无需重启。

    ★ **cron 驱动**（同 RSS 轮询的 E-9 模式）：``next_delay`` 闭包每次
    触发后重读 ``emby.cron``，改表达式无需重启。未配 cron → 默认
    15 分钟（E-6 现行为不变）；``interval`` 仍作 ``EmbyScheduler``
    的字段保留（cron 模式下被 ``next_delay`` 忽略），供测试断言用。
    """
    from bt169.emby import EmbyScheduler, EmbySyncer

    def build():
        from bt169.api.routes.emby import build_client
        from bt169.repo.posts import PostRepo
        from bt169.repo.settings import SettingsRepo

        return EmbySyncer(
            client=build_client(SettingsRepo(app.state.db, app.state.box)),
            posts_repo=PostRepo(app.state.db),
        )

    # 哨兵：负数表示「用默认间隔」。0 已在 _make_lifespan 拦掉。
    return EmbyScheduler(
        build_syncer=build,
        interval=SYNC_INTERVAL_SECONDS if interval < 0 else interval,
        next_delay=lambda: emby_delay_seconds(_emby_cron(app)),
    )


def _emby_cron(app: FastAPI) -> str:
    """读 ``emby.cron``。取不到（密钥坏了/库锁了）→ 当作未配置。"""
    from bt169.repo.settings import SettingsRepo

    try:
        return SettingsRepo(app.state.db, app.state.box).get(
            section_key("emby", "cron")) or ""
    except Exception as exc:  # noqa: BLE001 — 后台线程，绝不抛
        log.warning("读取 Emby 定时刷新表达式失败：%s", exc)
        return ""


def emby_delay_seconds(
    expression: str | None, *, wall: Any = None
) -> float:
    """算出「距下次入库状态刷新还有多少秒」。**恒有返回值，绝不抛。**

    - 表达式为空 → :data:`EMBY_FALLBACK_SECONDS`（默认 15 分钟，E-6）
    - 表达式非法 → 同上 + 日志（定时器是后台线程，不能抛）
    - 否则 → 下一个 cron 时刻 − 现在，**恒为正且 ≥ 1 秒**

    ★ 与 :func:`poll_delay_seconds` 同源同规则，只差空值语义：
    RSS 空 cron 是「醒来但不干活」（兜底 60 s），Emby 空 cron 是
    「按默认间隔干」（900 s）——RSS 抓论坛烧额度，多醒无害；Emby
    同步只读本地 + 一次 Emby API，按默认节奏跑就是正确的现状。
    """
    expr = (expression or "").strip()
    if not expr:
        return EMBY_FALLBACK_SECONDS

    from bt169.source.cron import CronError, parse_cron

    try:
        cron = parse_cron(expr)
    except CronError as exc:
        log.warning("Emby 定时刷新表达式非法（%r）：%s", expr, exc)
        return EMBY_FALLBACK_SECONDS

    if wall is None:
        import time
        wall = time.time

    now = datetime.fromtimestamp(wall(), TZ_ARCHIVE)
    nxt = cron.next_after(now)
    if nxt is None:
        log.warning("Emby 定时刷新表达式不可达（%r）", expr)
        return EMBY_FALLBACK_SECONDS
    return max(1.0, (nxt - now).total_seconds())


def _build_renew_loop(app: FastAPI, interval: float):
    """会话续期定时器（W-16）。

    ★ 默认间隔 **12 小时**，而不是「每天一次」：
      ``maybe_renew`` 本身会判断是否进入续期窗口，多跑几次是幂等的；
      而跑得太稀疏（比如每天一次）可能让会话在两次检查之间就过期了。

    ★ 复用 ``EmbyScheduler``：它已经处理好了「不抛异常 + 算下次该跑的时刻
      + stop_event 能打断等待」。这里只需要把 ``build_syncer`` 换成
      「返回一个带 ``sync()`` 的适配器」——不重写一个定时器。
    """
    from bt169.emby import EmbyScheduler

    class _Adapter:
        def sync(self):
            from bt169.api.routes.status import build_renewer

            result = build_renewer(app).maybe_renew()
            if result.attempted:
                log.info("会话续期：ok=%s %s", result.ok, result.message)
            return result

    # ★ delay_first=True：服务器重启是常见操作，启动即续期会在反复重启中
    #   把登录额度烧光（REQUIREMENTS.md §B.2.2）。首次检查推迟一个 interval。
    return EmbyScheduler(build_syncer=lambda: _Adapter(), interval=interval,
                         delay_first=True, name="session-renew")


#: 会话续期的检查间隔（秒）。12 小时。
RENEW_CHECK_SECONDS = 12 * 3600

#: RSS 轮询的检查间隔（秒）。需求 C-1 建议 5–10 分钟，这里取 5 分钟。
#: 20 条窗口 ÷ 41 帖/天峰值 → 每天 288 轮 × 20 = 5760 条容量，余量充足。
POLL_CHECK_SECONDS = 5 * 60


def _ensure_runner(app: FastAPI):
    """把采集 runner 装配到 ``app.state``（幂等）。

    ★ 必须与手动采集共用**同一个** runner：

    - 它持有「采集通道」的锁，RSS 轮询靠 :meth:`CollectRunner.try_begin`
      与手动采集真互斥。各建一个 runner 就是各有一把锁，两边会同时跑
      采集，而「采集并发度恒为 1」是封号风险的硬约束。
    - 它缓存了 ``ForumClient``（带 Cookie、带限速器）。两个实例意味着
      两套限速器 → 限速形同虚设。

    本函数在装配期调用（而不只是在首个请求里惰性建），这样轮询线程
    拿到的一定是同一个对象。
    """
    if getattr(app.state, "collect_runner", None) is not None:
        return
    from bt169.api.routes.collect import build_runner

    app.state.collect_runner = build_runner(app)


def _install_poll_scheduler(app: FastAPI, interval: float) -> None:
    """装配 RSS 轮询定时器（C-1）。

    ★ **必须先把 runner 放进 state**：轮询器要拿它做采集通道互斥。
      否则轮询会另建一个 collector（另一套限速器），而锁也各是一把。
    """
    if not interval:
        return
    _ensure_runner(app)
    app.state.poll_scheduler = _build_poll_loop(app, _poll_interval(interval))


def _build_poll_loop(app: FastAPI, interval: float):
    """RSS 轮询定时器（C-1）。

    ★ 复用 ``EmbyScheduler``：它已经处理好了「不抛异常 + 算下次该跑的时刻
      + stop_event 能打断等待」。

    ★ ``delay_first=True``：服务器重启是常见操作。RSS 不烧登录额度
      （事实 #21），但启动即抓帖意味着每次重启都产生一轮真实论坛请求
      + 2–5 秒限速。推迟一个 interval 更稳。

    ★ 每次 tick **重建** poller 与 feed 客户端：用户改了 RSS 链接
      或账号密码后无需重启服务。

    ★ **等待时长改由 cron 表达式决定**（E-9，``site.rss_cron``）：
      ``interval`` 已**不再用于 RSS**，只保留形参以兼容现有调用点。
      未配 cron → 返回**兜底间隔**（醒来但不干活），因此填上表达式
      **无需重启**即生效。这符合「后台定时器默认必须关闭」——
      空 cron 时不抓帖，但线程保持活着以接住新配置。
    """
    from bt169.emby import EmbyScheduler

    class _Adapter:
        def sync(self):
            from bt169.api.routes.collect import build_poller

            # ★ 空 cron = 不做定时检查。这里是「是否干活」的判断点，
            #   与「何时醒来」（``next_delay``）分开：醒来很便宜
            #   （只读一次设置），抓帖不便宜（真请求论坛 + 限速）。
            if not _rss_cron(app).strip():
                return None

            poller = build_poller(app)
            if poller is None:
                return None          # 未配置订阅链接 → 静默跳过

            # ★ DEBUG 级：cron 触发时留个痕迹。否则「发现 0 个新帖」
            #   这条路径在日志与库里都没任何痕迹，用户问「为什么不
            #   自动检查」时无从诊断。
            log.debug("RSS 订阅检查：cron 触发")
            result = poller.tick()
            if result.new:
                log.info("RSS 轮询：发现 %s 个新帖，采集 %s 个%s",
                         result.new, result.collected,
                         f"，错误：{result.error}" if result.error else "")
            return result

    # ★ cron 驱动（E-9）：每次触发后从库里重读表达式。
    #   用闭包重读而不是启动时快照——用户在设置页改完就生效，不必重启。
    def delay() -> float | None:
        return poll_delay_seconds(_rss_cron(app))

    return EmbyScheduler(build_syncer=lambda: _Adapter(), interval=interval,
                         delay_first=True, name="rss-poll",
                         next_delay=delay)


def _rss_cron(app: FastAPI) -> str:
    """读 ``site.rss_cron``。取不到（密钥坏了/库锁了）→ 当作未配置。"""
    from bt169.repo.settings import SettingsRepo

    try:
        return SettingsRepo(app.state.db, app.state.box).get(
            section_key("site", "rss_cron")) or ""
    except Exception as exc:  # noqa: BLE001 — 后台线程，绝不抛
        log.warning("读取 RSS 订阅检查表达式失败：%s", exc)
        return ""


def poll_delay_seconds(
    expression: str | None, *, wall: Any = None
) -> float:
    """算出「距下次订阅检查还有多少秒」。**恒有返回值，绝不抛。**

    - 表达式为空 → :data:`POLL_FALLBACK_SECONDS`
    - 表达式非法 → 同上 + 日志（定时器是后台线程，不能抛）
    - 否则 → 下一个 cron 时刻 − 现在，**恒为正**

    ★ **空表达式返回兜底间隔，而不是让定时器退出**：若返回 ``None``
      使线程结束，用户之后在设置页填上表达式就必须重启服务才生效——
      而本项目已确立「改了配置无需重启」（每次 tick 重建 poller 就是
      这个理由）。「何时醒来」与「是否干活」是两件事，分开处理。

    ★ 用 ``time.time()``（墙上时钟）而非 ``time.monotonic()``：
      cron 是日历语义，必须与墙上时间对齐。

    ★ 结果夹到最小 1 秒：NTP 回拨时若返回 0 或负数，
      ``_wait_until`` 会立刻返回 → 忙循环疯狂抓论坛。
    """
    expr = (expression or "").strip()
    if not expr:
        return POLL_FALLBACK_SECONDS

    from bt169.source.cron import CronError, parse_cron

    try:
        cron = parse_cron(expr)
    except CronError as exc:
        log.warning("RSS 订阅检查表达式非法（%r）：%s", expr, exc)
        return POLL_FALLBACK_SECONDS

    if wall is None:
        import time
        wall = time.time

    now = datetime.fromtimestamp(wall(), TZ_ARCHIVE)
    nxt = cron.next_after(now)
    if nxt is None:
        log.warning("RSS 订阅检查表达式不可达（%r）", expr)
        return POLL_FALLBACK_SECONDS
    return max(1.0, (nxt - now).total_seconds())


#: 负数 = 「用默认间隔」哨兵（与 ``_build_emby_scheduler`` 一致）。
def _poll_interval(interval: float) -> float:
    return POLL_CHECK_SECONDS if interval < 0 else interval


#: 未配置 cron 表达式时的**醒来**间隔（E-9）。
#:
#: ★ 这是「何时醒来」而非「多久抓一次」——醒来后 ``_Adapter.sync``
#:   会看到 cron 为空并直接返回。
#:
#: ★ 选 60 秒而非 5 分钟：醒来成本只是**读一次 SQLite 设置**（微秒级），
#:   但用户填完表达式后要等满这个间隔才生效。5 分钟太长，会让人以为
#:   「填了没用」而反复保存、或以为要重启服务。
POLL_FALLBACK_SECONDS = 60


#: Emby 定时刷新未配 cron 时的间隔。即 E-6 的默认节奏（15 分钟），
#: 也就是 SYNC_INTERVAL_SECONDS —— 单独命名是为了让「空 cron = 默认」
#: 这个语义在 emby_delay_seconds 里自解释。
EMBY_FALLBACK_SECONDS = SYNC_INTERVAL_SECONDS


def _install_emby_scheduler(app: FastAPI, interval: float) -> None:
    """兼容入口：定时器由 lifespan 启动，这里只保留 state 便于单测断言。

    ★ 不在这里启动：``create_app`` 返回时还没进入 lifespan，此时
    启动会让测试里的 ``TestClient`` 之外也跑起后台线程。
    """
    if not interval:
        return
    app.state.emby_scheduler = _build_emby_scheduler(app, interval)


class _ImmutableStaticFiles(StaticFiles):
    """给本地化图片加长缓存头。

    文件名是内容派生的（``{sha1(url)[:16]}-{width}.webp``），同一 URL
    同一宽度的产物**永远不变**，所以可以放心让浏览器长期缓存：
    省掉卡片滚动时的重复请求，也不怕内容过期。

    ★ 只有这一条路径能这样干。前端 HTML/JS/CSS 的文件名不带哈希，
    用同一策略会导致改完代码刷新看不到变化。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):  # type: ignore[no-untyped-def]
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def _install_error_handler(app: FastAPI) -> None:
    """统一错误响应形状：``{"error": {"code", "message"}}``。

    前端 ``api.js`` 依赖这个形状做错误归一化（`FRONTEND.md` §3.1）。
    """

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = {"error": detail}
        else:
            body = {"error": {"code": "http_error", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def unhandled(request, exc: Exception):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": str(exc)}},
        )
