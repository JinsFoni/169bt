"""FastAPI 应用装配。"""

from __future__ import annotations

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


def create_app(
    db: Database,
    *,
    box: SecretBox,
    ui_dir: Path | None = UI_DIR,
    image_dir: Path | None = IMAGE_DIR,
) -> FastAPI:
    """装配应用。

    Args:
        db: 已迁移的数据库。
        box: 设置密钥字段的加解密器。
        ui_dir: 前端目录；``None`` 表示不挂载静态资源（API 单测用）。
        image_dir: 本地化图片目录（W-15）；``None`` 表示不挂载 ``/img``。
    """
    app = FastAPI(
        title="169bt 归档台",
        version=__version__,
        docs_url=None,          # 个人自用，不需要 Swagger UI 暴露面
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db = db
    app.state.box = box
    app.state.image_dir = image_dir

    _install_error_handler(app)

    from bt169.api.gate import GateMiddleware
    from bt169.api.routes import (
        auth,
        collect,
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
