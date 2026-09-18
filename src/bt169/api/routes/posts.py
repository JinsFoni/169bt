"""只读浏览接口：日期列表与按日期取帖，以及删除。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from bt169.api.deps import get_archive, get_images, get_posts
from bt169.collector.imagecache import ImageCache
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


def _delete_or_404(
    posts: PostRepo, tid: int, images: ImageCache | None = None
) -> None:
    """硬删除（ADR-7）：先删行、提交事务，**再**删图片。

    ★ 顺序很重要（`ARCHITECTURE.md` §5.8）：

    - 先删行再删文件 → 中途失败只会留下**孤儿文件**（``169bt doctor`` 可清）
    - 反过来 → 中途失败会留下**指向不存在文件的数据库行** = 裂图

    ★ 图片要做**引用计数**：同一张图可能被多个帖子引用（同番号多帖、
    图床复用），计数不为 0 就不能删文件。计数依据是**源 URL**——
    因为 ``key = sha1(源 URL)``，同 key 必然来自同 URL。
    """
    post = posts.get(tid)
    if post is None or not posts.delete(tid):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"帖子不存在：{tid}"},
        )

    if images is None:
        return
    for url in (post.cover_img, post.detail_img):
        if url and not posts.references_image(url):
            images.delete(url)


@router.delete("/api/posts/{tid}", status_code=204)
def delete_post(
    tid: int,
    posts: PostRepo = Depends(get_posts),
    images: ImageCache | None = Depends(get_images),
) -> None:
    """硬删除一帖（行 + 图片）。

    前端有 5 s 撤销窗口（`FRONTEND.md` §13）——**窗口内不发请求**，
    因此这里的删除是立即且不可逆的。
    """
    _delete_or_404(posts, tid, images)


@router.post("/api/posts/{tid}/delete", status_code=204)
def delete_post_beacon(
    tid: int,
    posts: PostRepo = Depends(get_posts),
    images: ImageCache | None = Depends(get_images),
) -> None:
    """``sendBeacon`` 兜底端点（页面关闭时无法等待 fetch 完成）。

    语义与 ``DELETE /api/posts/{tid}`` **完全等价**——``sendBeacon`` 只能发
    POST，且无法设置请求头或读取响应。
    """
    _delete_or_404(posts, tid, images)
