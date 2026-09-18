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

    from bt169.api.routes import collect, health, posts, settings, status

    app.include_router(health.router)
    app.include_router(posts.router)
    app.include_router(settings.router)
    app.include_router(collect.router)
    app.include_router(status.router)

    # 静态资源必须**最后**挂载：Starlette 按注册顺序匹配，
    # 挂在 "/" 的 StaticFiles 会吞掉之后注册的所有路由。
    #
    # ``/img`` 是本地化图片（W-15）。文件名含内容哈希，所以可以
    # 永久缓存（``immutable``）—— 换图必然换文件名。
    if image_dir is not None and Path(image_dir).is_dir():
        app.mount(
            "/img",
            StaticFiles(directory=str(image_dir)),
            name="images",
        )
    if ui_dir is not None and Path(ui_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


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
