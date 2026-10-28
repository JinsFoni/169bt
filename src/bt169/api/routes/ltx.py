"""LTX 数据源接口（第二个 ed2k 来源：AVdb 的「LTX破解」板块）。

五条只读接口 + 两条写接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | ``/api/ltx/status`` | 是否配置好、上次同步时间、本地条数 |
| POST | ``/api/ltx/sync`` | 从 AVdb 全量拉取并更新本地镜像 |
| GET  | ``/api/ltx/dates`` | 有资源的日期与条数（对齐 ``/api/dates``） |
| GET  | ``/api/ltx/posts`` | 某日资源（对齐 ``/api/posts``） |
| GET  | ``/api/ltx/cover/{id}`` | 封面图（按需下载+本地化，浏览器并行取） |
| GET  | ``/api/ltx/{id}/actress`` | 演员（灯箱按需查 javdb，进程内缓存） |
| POST | ``/api/ltx/{id}/forward`` | 单条转发 ed2k 到 Telegram |
| POST | ``/api/ltx/forward?date=`` | 批量转发当日全部 ed2k |

★ **为什么不直接每次翻页都问 AVdb**：见 ``migrations/007_ltx.sql`` 的
  开头说明（日期计数、已下载状态、离线可看）。这里对外暴露的是**镜像**，
  同步是显式动作（前端在 LTX 页发现过期时触发），浏览路径零网络往返。

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
from bt169.api.deps import get_ltx, get_settings
from bt169.api.routes.telegram import BatchResult, ForwardResult, _fail
from bt169.avdb import (
    AvdbAuthError,
    AvdbCreds,
    AvdbError,
    AvdbNotConfigured,
    LtxSource,
    image_fetch,
    load_creds,
    movie_actress,
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

#: 演员查询的**上游串行闸门**。实测 6 路并发打 javdb 详情 → 40/40 全 502
#: （走的是用户自己的 javdb 账号，有额度），串行 +0.5 秒间隔则全成功。
_actress_lock = threading.Lock()

#: 抢不到闸门时最多等多久（秒）。灯箱一次只开一个，2 秒够正常使用；
#: 连点开一堆时不排队，直接回 ``busy``，避免请求雪崩。
_ACTRESS_WAIT = 2.0

#: ``item_id → (detail_url, number, actress)``。只缓存**已问过**的结果
#: （含「查不到」），上游失败不写缓存。进程内、重启即失效，够用了。
_actress_cache: dict[int, tuple[str | None, str | None, str | None]] = {}
_ACTRESS_CACHE_MAX = 500


def get_source(request: Request) -> LtxSource:
    """构造 LTX 数据源（每次请求都读最新设置）。"""
    return LtxSource(
        SettingsRepo(request.app.state.db, request.app.state.box)
    )


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
    source: LtxSource = Depends(get_source),
    ltx: LtxRepo = Depends(get_ltx),
) -> dict[str, Any]:
    """LTX 数据源概况。前端据此决定「要不要先同步」。"""
    synced_at = source.synced_at()
    return {
        "configured": source.configured(),
        "synced_at": synced_at,
        "count": ltx.count(),
        "ttl": config.LTX_SYNC_TTL,
    }


@router.post("/sync")
def sync(
    source: LtxSource = Depends(get_source),
    ltx: LtxRepo = Depends(get_ltx),
) -> dict[str, Any]:
    """从 AVdb 全量拉取 LTX 板块，覆盖本地镜像。

    ★ **同步而非后台任务**：7 页 ≈ 3 秒，用户可以等。与采集（几十分钟、
      必须后台线程）不是一个量级。
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

    log.info("LTX 同步完成：%s 条入库，%s 条移除", written, pruned)
    return {
        "ok": True,
        "fetched": written,
        "pruned": pruned,
        "count": ltx.count(),
        "synced_at": fetched_at,
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
    source: LtxSource, creds: AvdbCreds, item: LtxItem
) -> tuple[str | None, str | None]:
    """查演员，401 时用凭据重登**一次**再试（与 ``fetch_all`` 同策略）。

    ★ ``/api/v1/javdb/*`` 在 OpenAPI 里写 ``security: None``，实测不带 JWT
      就是 401（AVdb 有全局鉴权中间件）。所以这条路径也必须有令牌。
    """
    token = source.token()
    try:
        return movie_actress(
            creds.base_url,
            detail_url=item.detail_url,
            number=item.number,
            token=token,
        )
    except AvdbAuthError:
        log.info("LTX 演员：JWT 失效，重新登录后重试")
        token = source.token(force=True)
        return movie_actress(
            creds.base_url,
            detail_url=item.detail_url,
            number=item.number,
            token=token,
        )


@router.get("/{item_id}/actress")
def actress(
    item_id: int,
    request: Request,
    ltx: LtxRepo = Depends(get_ltx),
    source: LtxSource = Depends(get_source),
) -> dict[str, Any]:
    """这条资源的演员（灯箱打开时才查，**不落库**）。

    为什么是「按需」而不是同步时补全，见 ``avdb.movie_actress`` 的实测记录
    （未缓存 0.6–14 秒、并发即 502）。这里只额外做两件事：

    1. **进程内缓存**：同一条二次打开 / 刷新页面不再问上游（AVdb 自己的
       缓存 10–15 ms，我们连这一跳也省掉）；
    2. **上游串行闸门**：实测 6 路并发 → 40/40 全 502。灯箱一次只开一个，
       串行化的代价就是「连点开好几个时，后面的等前面」；等不到（2 秒）
       就回 ``busy``，前端当作「没有演员」静默处理，绝不排队堆请求。

    查不到不是错误：``actress: null``。上游真失败才 502（与其它 LTX 接口
    同一个错误口径，前端也是同一个处理）。
    """
    item = ltx.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"LTX 资源不存在：{item_id}"},
        )

    cached = _actress_cache.get(item_id)
    # ★ 缓存键带上 ``detail_url``/``number``：重新同步后编号或指向变了，
    #   旧结果就不能再用（否则会一直显示上一版的女优）。
    if cached is not None and cached[:2] == (item.detail_url, item.number):
        return {"ok": True, "actress": cached[2], "cached": True}

    creds = load_creds(SettingsRepo(request.app.state.db, request.app.state.box))
    if not _actress_lock.acquire(timeout=_ACTRESS_WAIT):
        log.info("LTX 演员：上游正忙，跳过 %s（%s）", item_id, item.number)
        return {"ok": True, "actress": None, "busy": True}

    try:
        names, movie_id = _lookup_actress(source, creds, item)
    except AvdbError as exc:
        log.info("LTX 演员查询失败（%s %s）：%s", item_id, item.number, exc)
        raise _upstream(exc) from exc
    finally:
        _actress_lock.release()

    if len(_actress_cache) >= _ACTRESS_CACHE_MAX:
        _actress_cache.clear()          # 无上限字典会被长跑进程撑爆
    _actress_cache[item_id] = (item.detail_url, item.number, names)

    log.info(
        "LTX 演员：%s（%s）→ %s", item.number, movie_id, names or "（未收录）"
    )
    return {"ok": True, "actress": names, "movie_id": movie_id, "cached": False}


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
