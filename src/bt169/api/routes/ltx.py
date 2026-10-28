"""LTX 数据源接口（第二个 ed2k 来源：AVdb 的「LTX破解」板块）。

五条只读接口 + 三条写接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | ``/api/ltx/status`` | 是否配置好、上次同步时间、条数、演员补齐进度 |
| POST | ``/api/ltx/sync`` | 从 AVdb 全量拉取并更新本地镜像（顺带触发演员回填） |
| GET  | ``/api/ltx/dates`` | 有资源的日期与条数（对齐 ``/api/dates``） |
| GET  | ``/api/ltx/posts`` | 某日资源（对齐 ``/api/posts``），含已落库的演员 |
| GET  | ``/api/ltx/cover/{id}`` | 封面图（按需下载+本地化，浏览器并行取） |
| GET  | ``/api/ltx/{id}/actress`` | 单条演员：库里没有时立刻查一次并落库（灯箱插队） |
| POST | ``/api/ltx/actress/backfill`` | 启动后台回填（幂等；前端打开 LTX 页时按需叫一次） |
| POST | ``/api/ltx/{id}/forward`` | 单条转发 ed2k 到 Telegram |
| POST | ``/api/ltx/forward?date=`` | 批量转发当日全部 ed2k |

★ **为什么不直接每次翻页都问 AVdb**：见 ``migrations/007_ltx.sql`` 的
  开头说明（日期计数、已下载状态、离线可看）。这里对外暴露的是**镜像**，
  同步是显式动作（前端在 LTX 页发现过期时触发），浏览路径零网络往返。

★ **演员为什么要单独回填**（L-12）：AVdb 的资源对象里没有演员字段，得逐条
  去它的 javdb 模块问；一条 0.6–14 秒且**不能并发**（并发即 502）。所以
  同步只拉列表，演员由 :mod:`bt169.actress` 在后台串行补进 ``ltx_items``，
  卡片和灯箱随后都直接读库。见 ``migrations/008_ltx_actress.sql``。

★ **失败方向**：AVdb 挂了只影响「同步」这一个动作。已有的镜像照常可翻，
  这也是镜像表的另一半理由。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from bt169 import config
from bt169.actress import GATE, GATE_WAIT
from bt169.api.deps import get_ltx, get_settings
from bt169.api.routes.telegram import BatchResult, ForwardResult, _fail
from bt169.avdb import (
    AvdbAuthError,
    AvdbError,
    AvdbNotConfigured,
    LtxSource,
    image_fetch,
    load_creds,
)
from bt169.collector.imagecache import (
    CARD_WIDTH,
    LIGHTBOX_WIDTH,
    ImageCache,
    ImageError,
)
from bt169.models import LtxItem, LtxPostDTO
from bt169.mtpproto import MtpNotConfigured, get_sender
from bt169.repo.ltx import LtxRepo
from bt169.repo.posts import now_iso
from bt169.repo.settings import SettingsRepo
from bt169.telegram import TelegramError, forward_ltx, forward_ltx_many

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ltx", tags=["ltx"])

#: 同一时刻只允许一个同步在跑。
#:
#: ★ 前端在「镜像过期」时会自动触发同步，多个标签页/多次刷新可能同时
#:   打进来。并发同步不只是浪费——它们会各自翻 7 页、各自整表 upsert，
#:   相互覆盖 ``fetched_at``，还可能把 AVdb 打成待审对象。
#:   抢不到锁就回 409，让前端等下一次。
_sync_lock = threading.Lock()

#: 允许的封面档位（与前端灯箱/卡片一致）。
_COVER_WIDTHS = (CARD_WIDTH, LIGHTBOX_WIDTH)

#: 演员查询的上游串行闸门在 :mod:`bt169.actress` 里（回填线程与下面的
#: 按需查询**必须**共用一把，否则两条路径同时打 javdb 就是「并发」）。
#: 抢不到闸门最多等 :data:`GATE_WAIT` 秒，等不到就回 ``busy``，不排队。


def get_source(request: Request) -> LtxSource:
    """构造 LTX 数据源（每次请求都读最新设置）。"""
    return LtxSource(
        SettingsRepo(request.app.state.db, request.app.state.box)
    )


def _backfiller(request: Request) -> Any:
    """取后台演员回填器；没启用后台定时器时是 ``None``（单测即如此）。"""
    return getattr(request.app.state, "actress_backfiller", None)


def _bad_date(date: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": "bad_date",
            "message": f"日期格式必须是 YYYY-MM-DD：{date!r}",
        },
    )


def _upstream(exc: AvdbError) -> HTTPException:
    """AVdb 侧失败 → HTTP。用户能看懂的 message 原样带上。"""
    if isinstance(exc, AvdbNotConfigured):
        return HTTPException(
            status_code=400,
            detail={"code": "ltx_not_configured", "message": str(exc)},
        )
    if isinstance(exc, AvdbAuthError):
        # ★ 502 而非 401：401 在本应用里等于「门禁没过」，前端会去弹登录框。
        #   这里失败的是**上游账号**，与访问密码无关。
        return HTTPException(
            status_code=502,
            detail={"code": "ltx_auth", "message": str(exc)},
        )
    return HTTPException(
        status_code=502,
        detail={"code": "ltx_upstream", "message": str(exc)},
    )


# ---------------------------------------------------------------- 状态 / 同步


@router.get("/status")
def status(
    request: Request,
    source: LtxSource = Depends(get_source),
    ltx: LtxRepo = Depends(get_ltx),
) -> dict[str, Any]:
    """LTX 数据源概况。前端据此决定「要不要先同步」「要不要叫回填」。"""
    synced_at = source.synced_at()
    done, pending = ltx.actress_stats()
    backfiller = _backfiller(request)
    return {
        "configured": source.configured(),
        "synced_at": synced_at,
        "count": ltx.count(),
        "ttl": config.LTX_SYNC_TTL,
        # 演员补齐进度：``actress_pending > 0`` 说明还有卡片的演员名在路上，
        # 前端会叫一次回填并短暂轮询（见 ui/app.js 的 scheduleActressPoll）。
        "actress_done": done,
        "actress_pending": pending,
        "actress_running": bool(backfiller and backfiller.running),
    }


@router.post("/actress/backfill")
def actress_backfill(
    request: Request,
    ltx: LtxRepo = Depends(get_ltx),
) -> dict[str, Any]:
    """启动后台演员回填（幂等）。

    三个触发点：同步成功后自动叫一次、前端打开 LTX 页发现
    ``actress_pending > 0`` 时叫一次、用户在设置页手动同步后叫一次。
    幂等很重要——多个标签页同时打开时不要起两个回填线程（它们会各自
    打上游，结果就是 502）。

    ★ 没有启用后台定时器（单测、``--no-cron`` 之类的装配）时这里是空操作，
      回 ``running: false`` 而已：演员会退化成「打开灯箱时现查一条」。
    """
    backfiller = _backfiller(request)
    started = bool(backfiller and backfiller.start())
    done, pending = ltx.actress_stats()
    return {
        "ok": True,
        "started": started,
        "running": bool(backfiller and backfiller.running),
        "actress_done": done,
        "actress_pending": pending,
    }


@router.post("/sync")
def sync(
    request: Request,
    source: LtxSource = Depends(get_source),
    ltx: LtxRepo = Depends(get_ltx),
) -> dict[str, Any]:
    """从 AVdb 全量拉取 LTX 板块，覆盖本地镜像，然后触发演员回填。

    ★ **同步而非后台任务**：15 页 ≈ 3 秒，用户可以等。与采集（几十分钟、
      必须后台线程）不是一个量级。

    ★ **演员回填是后台的**：1450 条要串行问上游，实测 2–5 秒一条，
      跑完要一小时上下（还只能一条一条来），所以同步只负责把列表落库，回填由 :mod:`bt169.actress`
      在另一个线程里慢慢做。响应里带上 ``actress_pending``，前端据此
      显示进度并短暂轮询，不必傻等。
    """
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail={"code": "ltx_syncing", "message": "LTX 正在同步中，请稍候"},
        )
    try:
        rows = source.fetch_all()
        fetched_at = now_iso()
        written = ltx.upsert_many(rows, fetched_at)
        # ★ 只有拿到结果才修剪：上游临时返回空列表时照删不误，
        #   用户会看到整站资源凭空消失，下次同步又冒出来。
        pruned = ltx.prune([r["id"] for r in rows]) if rows else 0
        if rows:
            source.mark_synced(fetched_at)
    except AvdbError as exc:
        log.warning("LTX 同步失败：%s", exc)
        raise _upstream(exc) from exc
    finally:
        _sync_lock.release()

    # 新条目和新编号都要补演员。放在锁外：回填要跑半小时，别占着同步锁。
    backfiller = _backfiller(request)
    if backfiller is not None:
        backfiller.start()

    log.info("LTX 同步完成：%s 条入库，%s 条移除", written, pruned)
    _, actress_pending = ltx.actress_stats()
    return {
        "ok": True,
        "fetched": written,
        "pruned": pruned,
        "count": ltx.count(),
        "synced_at": fetched_at,
        "actress_pending": actress_pending,
    }


# ---------------------------------------------------------------- 浏览


@router.get("/dates")
def list_dates(ltx: LtxRepo = Depends(get_ltx)) -> list[dict[str, Any]]:
    """有资源的日期，**升序**（与 ``/api/dates`` 同约定，前端代码可复用）。"""
    return [{"date": d, "count": c} for d, c in ltx.list_dates()]


@router.get("/posts")
def list_posts(
    date: str = Query(..., description="发布日期 YYYY-MM-DD"),
    ltx: LtxRepo = Depends(get_ltx),
) -> list[dict[str, Any]]:
    """某日 LTX 资源，新导出的在前。

    未知日期返回空数组（与 ``/api/posts`` 一致：没有不是错误）。
    """
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise _bad_date(date)
    return [LtxPostDTO.from_item(i).to_dict() for i in ltx.list_by_date(date)]


@router.get("/cover/{item_id}")
def cover(
    item_id: int,
    request: Request,
    w: int = Query(CARD_WIDTH, description=f"宽度档位：{CARD_WIDTH} 或 {LIGHTBOX_WIDTH}"),
    ltx: LtxRepo = Depends(get_ltx),
) -> FileResponse:
    """封面图：首次请求时下载+转码+落盘，之后直接发文件。

    ★ 卡片不直接引用 AVdb/图床地址，原因有两条：
      1. 图床（dmm / tp.spfcas.com）在部分网络下不可达，而**后端**有
         应用内代理设置可用；
      2. 直连会把「AVdb 的图床」变成浏览前提，图床抖动时整页空白。
    与 4K 帖子的图片本地化（W-15）是同一个理由，只是这里**按需**做
    （上千条全部预下载要几百 MB，而用户只会看其中一部分）。

    ★ 用 ``FileResponse`` 而非 302 跳转：URL 里已经有稳定 id，
      直接回字节可以让浏览器按 immutable 缓存，少一次往返。
    """
    if w not in _COVER_WIDTHS:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_width", "message": f"宽度只能是 {list(_COVER_WIDTHS)}"},
        )

    item = ltx.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"LTX 资源不存在：{item_id}"},
        )
    if not item.cover_url:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_cover", "message": "这条记录没有封面图"},
        )

    image_dir = getattr(request.app.state, "image_dir", None)
    if image_dir is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_images", "message": "未启用图片目录"},
        )

    # ★ 走 AVdb 的图床代理，不直连图床：源图床（tp.spfcas.com）对直连
    #   返回的是加密字节（热链保护），PIL 直接报「不是有效图片」。见
    #   ``avdb.cover_proxy_url`` 的实测记录。
    creds = load_creds(SettingsRepo(request.app.state.db, request.app.state.box))
    base = creds.base_url

    cache = ImageCache(
        image_dir,
        fetch=lambda u: image_fetch(u, base_url=base),
        sizes=(CARD_WIDTH, LIGHTBOX_WIDTH),
    )
    try:
        cached = cache.ensure(item.cover_url)
    except ImageError as exc:
        log.info("LTX 封面下载失败（%s）：%s", item.cover_url, exc)
        raise HTTPException(
            status_code=404,
            detail={"code": "cover_failed", "message": str(exc)},
        ) from exc

    path = cache.path_for(cached.key, w)
    if not path.exists():       # 理论上刚 ensure 过；防缓存目录被手工清过
        raise HTTPException(
            status_code=404,
            detail={"code": "cover_missing", "message": "封面文件不存在"},
        )
    return FileResponse(
        path,
        headers={"cache-control": "public, max-age=31536000, immutable"},
    )


def _lookup_actress(
    source: LtxSource, item: LtxItem
) -> tuple[str | None, str | None]:
    """查一条演员（凭据、JWT、401 重登都在 ``LtxSource.lookup_actress``）。"""
    return source.lookup_actress(
        detail_url=item.detail_url, number=item.number
    )


@router.get("/{item_id}/actress")
def actress(
    item_id: int,
    ltx: LtxRepo = Depends(get_ltx),
    source: LtxSource = Depends(get_source),
) -> dict[str, Any]:
    """这条资源的演员（灯箱里那条**还没轮到回填**时，插队查一次并落库）。

    正常情况下这个接口根本不会被调用：演员已经由 :mod:`bt169.actress`
    在同步后落库，``/api/ltx/posts`` 直接带着 ``actress`` 回来。它存在的
    唯一理由是「用户点开的那条恰好在待办队列尾部」——与其让他盯着空位，
    不如立刻查这一条（1 秒内），顺手把结果写回库，让卡片也补上名字。

    三种回答（前端都不弹错误，演员是附带功能）：

    - 查到 / 确认没有 → ``actress`` 为该串或 ``null``；
    - 库里已有结论 → ``cached: true``，不问上游；
    - 上游正忙（回填线程占着闸门超过 :data:`GATE_WAIT` 秒）→ ``busy: true``。

    上游真失败才 502（与其它 LTX 接口同一个错误口径）。
    """
    item = ltx.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"LTX 资源不存在：{item_id}"},
        )

    # ★ 库里已有结论就用库里的（含「确认没有」）。这既是缓存也是唯一真相：
    #   回填线程写完库，这里读到的就是它查到的，不存在两套结果打架。
    if item.actress_at:
        return {
            "ok": True, "actress": item.actress, "movie_id": None, "cached": True,
        }

    if not GATE.acquire(timeout=GATE_WAIT):
        log.info("LTX 演员：上游正忙，跳过 %s（%s）", item_id, item.number)
        return {"ok": True, "actress": None, "busy": True}

    try:
        names, movie_id = _lookup_actress(source, item)
    except AvdbError as exc:
        log.info("LTX 演员查询失败（%s %s）：%s", item_id, item.number, exc)
        raise _upstream(exc) from exc
    finally:
        GATE.release()

    # ★ 落库（上游失败时不会走到这里）：这条从此不用再问，卡片也补上了名字。
    ltx.set_actress(item_id, names, now_iso())

    log.info(
        "LTX 演员：%s（%s）→ %s", item.number, movie_id, names or "（未收录）"
    )
    return {
        "ok": True, "actress": names, "movie_id": movie_id, "cached": False,
    }


# ---------------------------------------------------------------- 转发


@router.post("/{item_id}/forward", response_model=ForwardResult)
def forward_one(
    item_id: int,
    ltx: LtxRepo = Depends(get_ltx),
    settings: SettingsRepo = Depends(get_settings),
) -> ForwardResult:
    """单条转发（逻辑与 ``/api/posts/{tid}/forward`` 完全对称）。"""
    item = ltx.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"LTX 资源不存在：{item_id}"},
        )

    try:
        sender = get_sender(settings)
        message_id = forward_ltx(item, client=sender, ltx_repo=ltx)
    except (MtpNotConfigured, TelegramError) as exc:
        raise _fail(exc) from exc

    return ForwardResult(ok=True, message_id=message_id)


@router.post("/forward", response_model=BatchResult)
def forward_day(
    date: str = Query(..., description="发布日期 YYYY-MM-DD"),
    ltx: LtxRepo = Depends(get_ltx),
    settings: SettingsRepo = Depends(get_settings),
) -> BatchResult:
    """批量转发某日全部 ed2k（与 ``/api/archive/forward`` 同语义、同返回）。"""
    if len(date) != 10 or date[4] != "-" or date[7] != "-":
        raise _bad_date(date)

    rows = ltx.list_by_date(date)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_items", "message": "该日期没有 LTX 资源"},
        )

    try:
        sender = get_sender(settings)
    except (MtpNotConfigured, TelegramError) as exc:
        raise _fail(exc) from exc

    return BatchResult(**forward_ltx_many(rows, client=sender, ltx_repo=ltx))
