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


def get_sync_lock() -> threading.Lock:
    """同步互斥锁。

    ★ 模块级单例（而非 per-app）：手动刷新、定时器、采集完成回调
    三条路径都从这里拿同一把，才能真的串行。单用户单进程场景下，
    模块级与 app 级没有区别；分散在各处各建一把反而是隐患。
    """
    return _sync_lock


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
        log.info("手动刷新：库内 %s 条，检查 %s 帖，命中 %s%s",
                 result.library_items, result.checked, result.in_library,
                 f"，错误：{result.error}" if result.error else "")
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

    username = (settings.get(config.section_key("emby", "username")) or "").strip()

    try:
        rows, source, warning = _list_views(client, username)
    except EmbyError as exc:
        return {"ok": False, "error": str(exc), "libraries": []}

    return {"ok": True, "error": None, "libraries": rows,
            "source": source, "filtered_by": username or None,
            "warning": warning}


def _list_views(
    client: EmbyClient, username: str = ""
) -> tuple[list[dict[str, str]], str, str | None]:
    """拉媒体库列表，**尽量**按用户名过滤。

    ★ 这是「帮你少看几个库」，**不是安全边界**（用户拍板）。所以三个分支：

    1. 配了用户名且能在 Emby 里找到 → ``/Users/{id}/Views``，只给该用户
       可见的库。``source="user"``。
    2. 没配用户名 → ``/Library/VirtualFolders``（管理员视角，全部库）。
       ``source="all"``。
    3. 配了但找不到（含大小写不匹配）→ **退回全部库**并给出 warning。
       宁可多给几个库，也不要让设置页整个用不了。

    Returns:
        ``(rows, source, warning)``。

    Raises:
        EmbyError: 拿库列表本身失败（网络 / 非 200）。
    """
    if username:
        try:
            user_id = client.resolve_user_id(username)
            views = client.list_views(user_id)
            return _rows_from_views(views, key="Id"), "user", None
        except EmbyError as exc:
            # 找不到用户 / 拉视图失败 → 退回全部库，但要让设置页说清楚
            rows = _admin_views(client)
            return rows, "all", f"{exc}；已退回显示全部媒体库"

    return _admin_views(client), "all", None


def _rows_from_views(rows: list[dict[str, Any]], *, key: str) -> list[dict[str, str]]:
    """归一成 ``[{id, name}]``。

    ★ 两个端点的 id 字段名**不一样**：``/Users/{id}/Views`` 用 ``Id``，
      ``/Library/VirtualFolders`` 用 ``ItemId``。赌错顺序或字段名会静默
      得到空列表。
    """
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get(key):
            out.append({"id": str(row[key]),
                        "name": str(row.get("Name") or "未命名")})
    return out


def _admin_views(client: EmbyClient) -> list[dict[str, str]]:
    """管理员视角的全部库（``/Library/VirtualFolders``）。

    ★ 这个端点返回的是**顶层数组**（``/Items`` 返回对象），所以走 ``_get_raw``。
    """
    payload = client._get_raw("/Library/VirtualFolders", {})
    rows = payload if isinstance(payload, list) else (payload.get("Items") or [])
    return _rows_from_views(rows, key="ItemId")
