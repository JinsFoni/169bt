"""FastAPI 依赖注入。

所有依赖从 ``request.app.state`` 取——不引入全局单例，
这样测试可以并行创建多个 app 实例而互不干扰。
"""

from __future__ import annotations

from fastapi import Request

from bt169.collector.imagecache import ImageCache
from bt169.db import Database
from bt169.repo.archive import ArchiveRepo
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo

__all__ = ["get_db", "get_posts", "get_archive", "get_settings", "get_images"]


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_posts(request: Request) -> PostRepo:
    return PostRepo(request.app.state.db)


def get_archive(request: Request) -> ArchiveRepo:
    return ArchiveRepo(request.app.state.db)


def get_settings(request: Request) -> SettingsRepo:
    return SettingsRepo(request.app.state.db, request.app.state.box)


def get_images(request: Request) -> ImageCache | None:
    """本地化图片缓存。

    ``image_dir`` 为 ``None``（API 单测）时返回 ``None``，
    调用方据此跳过图片清理。
    """
    image_dir = getattr(request.app.state, "image_dir", None)
    if image_dir is None:
        return None
    return ImageCache(image_dir)
