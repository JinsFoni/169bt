"""Emby 入库标记接口（P7 / E-1~E-7 / 架构 §5.4）。

★ **浏览路径不在这里**：卡片上的「已入库」标记由 ``/api/posts`` 直接读
``posts.emby_status``（纯本地）。本模块只负责**同步**与**查询库列表**。

★ **同步失败不返回 5xx**：E-7 要求 Emby 不可达时「不报错」。这里返回
``200 {ok:false, error}``，让设置面板能显示原因，而前端不必处理错误分支。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bt169 import config
from bt169.emby import (
    EmbyClient,
    EmbyError,
    EmbyNotConfigured,
    EmbySyncer,
    default_http,
)
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/emby", tags=["emby"])

#: 同一时刻只允许一个同步在跑（手动刷新 + 定时器可能撞上）。
_sync_lock = threading.Lock()


def get_posts(request: Request) -> PostRepo:
    return PostRepo(request.app.state.db)


def get_settings(request: Request) -> SettingsRepo:
    return SettingsRepo(request.app.state.db, request.app.state.box)


def build_client(settings: SettingsRepo) -> EmbyClient:
    """从设置构造客户端。

    Raises:
        EmbyNotConfigured: 未配置地址或 API Key。
    """
    url = settings.get(config.section_key("emby", "url")) or ""
    key = settings.get(config.section_key("emby", "api_key")) or ""
    library = settings.get(config.section_key("emby", "library")) or ""
    return EmbyClient(url=url, api_key=key, http=default_http(),
                      library_id=library or None)


class SyncResponse(BaseModel):
    ok: bool
    checked: int = 0
    in_library: int = 0
    library_items: int = 0
    error: str | None = None


@router.post("/refresh", response_model=SyncResponse)
def refresh(
    posts: PostRepo = Depends(get_posts),
    settings: SettingsRepo = Depends(get_settings),
) -> SyncResponse:
    """手动触发一次同步（E-6）。

    ★ 未配置时返回 ``ok:false`` 而非 400：设置面板的「测试/刷新」按钮
    要**显示**原因，用错误状态码会让前端走通用错误分支。
    """
    if not _sync_lock.acquire(blocking=False):
        return SyncResponse(ok=False, error="已有同步在进行中")

    try:
        try:
            client = build_client(settings)
        except EmbyNotConfigured as exc:
            return SyncResponse(ok=False, error=str(exc))

        result = EmbySyncer(client=client, posts_repo=posts).sync()
        return SyncResponse(**result.to_dict())
    finally:
        _sync_lock.release()


@router.get("/libraries")
def libraries(
    settings: SettingsRepo = Depends(get_settings),
) -> dict[str, Any]:
    """列出可选媒体库（S-8），供设置页下拉框使用。

    ★ 失败返回 ``{ok:false, error, libraries:[]}`` 而非 5xx——
    设置面板要显示「连不上」而不是弹一个通用错误。
    """
    try:
        client = build_client(settings)
    except EmbyNotConfigured as exc:
        return {"ok": False, "error": str(exc), "libraries": []}

    try:
        rows = _list_views(client)
    except EmbyError as exc:
        return {"ok": False, "error": str(exc), "libraries": []}

    return {"ok": True, "error": None, "libraries": rows}


def _list_views(client: EmbyClient) -> list[dict[str, str]]:
    """拉用户视图（媒体库）列表。

    Emby 的 ``/Users/{userId}/Views`` 需要 userId；``/Library/VirtualFolders``
    则直接可用（管理员视角）。这里用后者——个人自用场景下 API Key 就是
    管理员权限，少一次往返、也少一个要配的字段。

    ★ 这个端点返回的是**顶层数组**（``/Items`` 返回对象），所以走 ``_get_raw``。
    """
    payload = client._get_raw("/Library/VirtualFolders", {})
    rows = payload if isinstance(payload, list) else (payload.get("Items") or [])
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("ItemId"):
            out.append({"id": str(row["ItemId"]),
                        "name": str(row.get("Name") or "未命名")})
    return out
