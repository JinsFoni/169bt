"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from bt169 import __version__
from bt169.config import IMAGE_DIR, UI_DIR
from bt169.crypto import SecretBox
from bt169.db import Database

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
    """构造 lifespan：启动/停止 Emby 定时器（E-6）。

    ★ 用 ``lifespan`` 而非已弃用的 ``on_event``：后者会在每次注册时
    追加处理器，且 FastAPI 已标记弃用。

    ★ 间隔为 0 时返回一个什么都不做的 lifespan——测试入口就是这种。
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
        try:
            yield
        finally:
            renewer.stop()
            scheduler.stop()

    return lifespan


def _build_emby_scheduler(app: FastAPI, interval: float):
    """构造定时同步器。每次 tick 重建同步器——用户在设置页改地址后无需重启。"""
    from bt169.emby import SYNC_INTERVAL_SECONDS, EmbyScheduler, EmbySyncer

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
    )


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
                         delay_first=True)


#: 会话续期的检查间隔（秒）。12 小时。
RENEW_CHECK_SECONDS = 12 * 3600


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
