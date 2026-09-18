"""只读浏览接口：日期列表与按日期取帖，以及删除。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from bt169.api.deps import get_archive, get_posts
from bt169.models import PostDTO
from bt169.repo.archive import ArchiveRepo
from bt169.repo.posts import PostRepo

router = APIRouter(tags=["posts"])

#: 归档日期必须是严格的 YYYY-MM-DD。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/api/dates")
def list_dates(archive: ArchiveRepo = Depends(get_archive)) -> list[dict[str, Any]]:
    """有帖的归档日期，**升序**（左旧右新）。

    前端用它同时得到两个东西：日期条的内容，以及**上一日/下一日**的目标
    ——因此不需要单独的 nav 接口（W-12 的「跳过空档」由前端在数组上自然满足）。
    """
    return [d.to_dict() for d in archive.list_dates()]


@router.get("/api/posts")
def list_posts(
    date: str = Query(..., description="归档日期 YYYY-MM-DD"),
    posts: PostRepo = Depends(get_posts),
) -> list[dict[str, Any]]:
    """某归档日的帖子，新帖在前。

    未知日期返回**空数组**而非 404——「这天没帖子」不是错误，
    前端不应把它当作失败处理。
    """
    if not _DATE_RE.match(date):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_date",
                "message": f"日期格式必须是 YYYY-MM-DD：{date!r}",
            },
        )

    return [
        PostDTO.from_post(p, emby_in_library=p.emby_status == "in_library").to_dict()
        for p in posts.list_by_date(date)
    ]


def _delete_or_404(posts: PostRepo, tid: int) -> None:
    """硬删除（ADR-7）。图片清理由 `imagecache` 在 P3 接入时补充。"""
    if not posts.delete(tid):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"帖子不存在：{tid}"},
        )


@router.delete("/api/posts/{tid}", status_code=204)
def delete_post(tid: int, posts: PostRepo = Depends(get_posts)) -> None:
    """硬删除一帖（行 + 图片）。

    前端有 5 s 撤销窗口（`FRONTEND.md` §13）——**窗口内不发请求**，
    因此这里的删除是立即且不可逆的。
    """
    _delete_or_404(posts, tid)


@router.post("/api/posts/{tid}/delete", status_code=204)
def delete_post_beacon(tid: int, posts: PostRepo = Depends(get_posts)) -> None:
    """``sendBeacon`` 兜底端点（页面关闭时无法等待 fetch 完成）。

    语义与 ``DELETE /api/posts/{tid}`` **完全等价**——``sendBeacon`` 只能发
    POST，且无法设置请求头或读取响应。
    """
    _delete_or_404(posts, tid)
