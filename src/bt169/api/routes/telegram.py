"""Telegram 转发接口（需求 T-1~T-8 / 架构 §5.5）。

★ **转发绝不影响采集与浏览**（T-8）：本模块的失败只返回错误响应，
不触碰采集器状态，也不抛到全局错误处理器之外。

★ **凭据每次都从设置里读**：不在应用启动时缓存——用户改了 Token 应当
立刻生效，而不是重启服务。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bt169 import config
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo
from bt169.telegram import (
    AlreadySent,
    RateLimited,
    TelegramClient,
    TelegramError,
    TelegramNotConfigured,
    forward_many,
    forward_post,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["telegram"])


# ---------------------------------------------------------------- 依赖


def get_posts(request: Request) -> PostRepo:
    return PostRepo(request.app.state.db)


def get_settings(request: Request) -> SettingsRepo:
    return SettingsRepo(request.app.state.db, request.app.state.box)


def build_client(settings: SettingsRepo) -> TelegramClient:
    """从设置构造客户端。

    Raises:
        TelegramNotConfigured: 未配置 Token 或 Chat ID（需求 T-4）。
    """
    token = settings.get(config.section_key("tg", "token")) or ""
    chat_id = settings.get(config.section_key("tg", "chat_id")) or ""
    return TelegramClient(token=token, chat_id=chat_id, http=httpx.Client())


# ---------------------------------------------------------------- 接口


class ForwardResult(BaseModel):
    ok: bool
    message_id: int = 0


class BatchResult(BaseModel):
    sent: int
    skipped: int
    failed: int
    errors: list[dict[str, Any]]


def _fail(exc: TelegramError) -> HTTPException:
    """把领域异常翻译成 HTTP 状态码。

    ★ 区分状态码是为了让前端能给出**不同的**提示（需求 T-6）：
    未配置 → 引导去设置；已发过 → 提示无需重发；限流 → 告诉用户等多久。
    """
    if isinstance(exc, TelegramNotConfigured):
        return HTTPException(
            status_code=400,
            detail={"code": "tg_not_configured", "message": str(exc)},
        )
    if isinstance(exc, AlreadySent):
        return HTTPException(
            status_code=409,
            detail={"code": "tg_already_sent", "message": str(exc)},
        )
    if isinstance(exc, RateLimited):
        return HTTPException(
            status_code=429,
            detail={
                "code": "tg_rate_limited",
                "message": str(exc),
                "retry_after": exc.retry_after,
            },
        )
    return HTTPException(
        status_code=502,
        detail={"code": "tg_error", "message": str(exc)},
    )


@router.post("/posts/{tid}/forward", response_model=ForwardResult)
def forward_one(
    tid: int,
    posts: PostRepo = Depends(get_posts),
    settings: SettingsRepo = Depends(get_settings),
) -> ForwardResult:
    """转发单个帖子的 ed2k 到 Telegram（T-1）。"""
    post = posts.get(tid)
    if post is None:
        raise HTTPException(status_code=404, detail="帖子不存在")

    try:
        client = build_client(settings)
        message_id = forward_post(post, client=client, posts_repo=posts)
    except TelegramError as exc:
        raise _fail(exc) from exc

    return ForwardResult(ok=True, message_id=message_id)


@router.post("/archive/forward", response_model=BatchResult)
def forward_day(
    date: str,
    posts: PostRepo = Depends(get_posts),
    settings: SettingsRepo = Depends(get_settings),
) -> BatchResult:
    """批量转发某日全部 ed2k（T-2）。

    ★ **同步**执行但会阻塞：每条间隔 ≥3 秒（限流），所以一天十几帖要
    几十秒。这与采集接口不同——采集是几十分钟级，必须后台线程；
    转发是分钟级，前端可以转圈等。若日后单日帖数变多，再改成后台任务。

    ★ **部分失败返回 200**：批量语义下「有些成功有些失败」是正常结果，
    不是 HTTP 错误。失败明细在 ``errors`` 里，前端逐条提示。
    """
    rows = posts.list_by_date(date)
    if not rows:
        raise HTTPException(status_code=404, detail="该日期没有帖子")

    try:
        client = build_client(settings)
    except TelegramNotConfigured as exc:
        raise _fail(exc) from exc

    # 只转发有 ed2k 的（T-5）；其余由 forward_many 内部报失败
    result = forward_many(rows, client=client, posts_repo=posts)
    return BatchResult(**result)


@router.post("/settings/test/telegram")
def test_telegram(
    settings: SettingsRepo = Depends(get_settings),
) -> dict[str, Any]:
    """发一条测试消息，验证 TG 配置可用。"""
    try:
        client = build_client(settings)
        client.test()
    except TelegramError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": "测试消息已发送"}
