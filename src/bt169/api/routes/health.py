"""健康检查。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from bt169 import __version__

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> dict[str, Any]:
    """存活探测。

    **不需要认证**：Lucky 反代与容器健康检查会匿名访问。
    只报告「进程活着」，不含任何敏感信息——会话/采集器状态在
    ``/api/status``（需要认证）。
    """
    return {"status": "ok", "version": __version__}
