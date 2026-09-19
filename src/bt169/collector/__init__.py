"""采集编排：按日期范围回填帖子。

**并发度恒为 1**（C-7）。整个流程是单线程串行的：

1. **发现阶段**：从列表页第 1 页开始按发帖时间**降序**翻页，
   收集 ``post_date`` 落在 ``[from, to]`` 内的 tid。
   因为排序降序，一旦某页最旧日期 < ``from`` 就可以停——
   这是「提前退出」，避免把整个版块翻穿（实测共 225 页）。
2. **抓取阶段**：逐个 tid 抓详情页。已在库里的（任意状态）跳过。
3. 每步都检查取消标志，让用户能中途停下。

跳过规则：**数据库里已有的就跳过**（用户原话）。含失败态——
即不会自动重试历史失败，用户可再次采集该区间来重试。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable
from datetime import date, timedelta

from bt169 import config
from bt169.repo.collect import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    CollectJob,
    CollectRepo,
)
from bt169.repo.posts import PostRepo, now_iso
from bt169.source.forum import FetchError, ForumClient, LoginRequired
from bt169.source.parse import parse_rss, parse_thread_detail
from bt169.source.thanks import ThanksClient, ThanksError

from bt169.collector.imagecache import (
    CARD_WIDTH,
    LIGHTBOX_WIDTH,
    ORIGINAL,
    ImageCache,
    ImageError,
    matches_width,
)

__all__ = [
    "Collector",
    "CollectResult",
    "CollectError",
    "date_range",
    "ValidateError",
    "RssPoller",
    "RssPollResult",
    "POLL_INTERVAL_SECONDS",
]

log = logging.getLogger(__name__)


class CollectError(RuntimeError):
    """采集失败。"""


class ValidateError(ValueError):
    """日期范围非法。"""


@dataclass(frozen=True, slots=True)
class CollectResult:
    """一次采集的结果汇总。"""

    job_id: int
    status: str
    total: int
    collected: int
    skipped: int
    failed: int
    pages: int
    message: str | None


#: 每个字段要写哪些列、各用哪一档。两处写入（新采 + 补图）共用，避免漂移。
#:
#: ★ 一字段对多列：卡片与灯箱需要不同分辨率，而**同一张源图**
#: 只需下载一次（``ensure`` 内部去重）。早先只写一列，结果要么
#: 卡片加载原图（单页 11 MB），要么灯箱放大糊图（实测放大 1.88×）。
FIELD_TARGETS: dict[str, tuple[tuple[str, int], ...]] = {
    "cover": (
        ("cover_local", CARD_WIDTH),      # 卡片缩略图
        ("cover_orig", ORIGINAL),          # 灯箱看原图
    ),
    "detail": (
        ("detail_local", LIGHTBOX_WIDTH),  # 中间档
        ("detail_orig", ORIGINAL),          # 灯箱看原图
    ),
}


def date_range(from_date: str, to_date: str) -> list[str]:
    """展开 ``[from, to]`` 为日期列表（含两端）。

    Raises:
        ValidateError: 格式非法、from > to，或跨度超过上限。
    """
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
    except ValueError as exc:
        raise ValidateError("日期必须是 YYYY-MM-DD 格式") from exc

    if start > end:
        raise ValidateError("起始日期不能晚于结束日期")

    span = (end - start).days + 1
    if span > config.MAX_BACKFILL_DAYS:
        raise ValidateError(
            f"日期跨度最多 {config.MAX_BACKFILL_DAYS} 天，当前 {span} 天"
        )
    return [(start + timedelta(days=i)).isoformat() for i in range(span)]


class Collector:
    """采集编排器。

    用法::

        c = Collector(client=fc, posts=post_repo, jobs=job_repo)
        job = c.run(from_date="2026-09-01", to_date="2026-09-14")
    """

    def __init__(
        self,
        *,
        client: ForumClient,
        posts: PostRepo,
        jobs: CollectRepo,
        thanks: ThanksClient | None = None,
        images: ImageCache | None = None,
        on_login_required=None,
    ) -> None:
        self._c = client
        self._posts = posts
        self._jobs = jobs
        # ★ 未注入时不自动建：匿名会话下感谢必然失败，不如显式报错。
        #   测试与匿名采集传 None，需要解锁的场景由 API 层注入。
        self._thanks = thanks
        # ★ 未注入则不做本地化（测试、离线环境）
        self._images = images
        self._on_login_required = on_login_required
        # 进度回调（CLI 用）。默认无操作，避免每帖都判 None。
        self._on_progress: Callable[[CollectJob], None] | None = None
        #: 任务成功收尾后的回调（**手动采集与 RSS 轮询两条路径共用**，
        #: 因为它们共用同一个 Collector 实例）。收到本次采到的 tid 列表。
        #: 供「采完立即检查 Emby 入库状态」用。**时序契约：回调返回
        #: 之后任务才落 done**，因此回调方看到 done 就能保证徽章已
        #: 写入库；回调异常只记日志，与 ``_on_progress`` 同理——
        #: 附属动作不该拖垮采集。
        self.on_job_done: Callable[[list[int]], None] | None = None

    # ---------------------------------------------------------------- 主流程

    def run(
        self,
        *,
        from_date: str,
        to_date: str,
        fid: str = config.DEFAULT_FID,
        job: CollectJob | None = None,
        on_progress: Callable[[CollectJob], None] | None = None,
    ) -> CollectResult:
        """执行一次采集（**阻塞**，调用方负责放到后台线程）。

        Args:
            job: 已建好的任务（由 :class:`CollectRunner` 预建以避免重复）。
                为 ``None`` 时自行创建。
            on_progress: 每处理完一帖回调一次（CLI 打印进度用）。
                回调抛异常**不会**中断采集——进度显示不该拖垮长任务。

        Raises:
            ValidateError: 日期非法。
            CollectError: 已有任务在跑。
        """
        days = date_range(from_date, to_date)
        if job is None:
            job = self._jobs.create(from_date=from_date, to_date=to_date, fid=fid)
        log.info("采集任务 %s 启动：%s → %s (fid=%s)",
                 job.id, from_date, to_date, fid)
        self._on_progress = on_progress
        try:
            return self._run_job(job, days)
        except Exception as exc:  # noqa: BLE001 —— 必须落库后再抛
            log.exception("采集任务 %s 异常", job.id)
            # ★ 内层可能已经写了更具体的终态与说明（例如「会话失效：…」）。
            #   这里只在任务仍在跑时才接手，否则会把好信息覆盖成
            #   一句干巴巴的异常字符串。
            current = self._jobs.get(job.id)
            if current is not None and current.is_running:
                self._jobs.finish(job.id, status=JOB_FAILED,
                                  message=str(exc)[:500])
            raise

    def _run_job(self, job: CollectJob, days: list[str]) -> CollectResult:
        wanted = set(days)

        # ---------------- 阶段 1：发现
        self._jobs.update(job.id, phase="listing",
                          message=f"正在翻页查找 {job.from_date} 起的帖子")
        items, pages = self._discover(job, wanted)

        self._jobs.update(
            job.id, phase="fetching", total=len(items), pages=pages,
            message=f"发现 {len(items)} 个帖子，开始抓取",
        )
        return self.fetch_items(job, items)

    # ------------------------------------------------------------ 抓取阶段

    def fetch_items(
        self, job: CollectJob, items: list[tuple[int, str]]
    ) -> CollectResult:
        """抓取一批 ``(tid, post_date)`` 并推进任务状态。

        抽出来给两条发现路径共用：

        - :meth:`_run_job` —— 翻版块列表页（历史回填 / 手动采集 C-6、C-10）
        - :class:`RssPoller` —— 读 RSS 订阅（定时轮询 C-1）

        两边的差别**只在怎么发现 tid**；抓取、限速、感谢解锁、图片本地化、
        跳过规则、取消、进度回调全部应该一模一样。复制一份到轮询里迟早会
        漂移（比如只修了手动路径的跳过规则）。
        """
        def _tick() -> None:
            """逐帖通知进度。

            ★ 传的是**实时 ``CollectJob``**（有 ``processed``/``total``），
            不是 ``CollectResult``——后者是跑完之后的汇总，没有「已处理
            多少」这个中间量。

            回调异常只记日志：进度显示坏了不该让长任务挂掉。
            """
            if self._on_progress is None:
                return
            try:
                current = self._jobs.get(job.id)
                if current is not None:
                    self._on_progress(current)
            except Exception:  # noqa: BLE001
                log.warning("进度回调异常（已忽略）", exc_info=True)

        collected_tids: list[int] = []
        for tid, post_date in items:
            if self._jobs.is_cancel_requested(job.id):
                current = self._jobs.get(job.id)
                done = current.processed if current else 0
                self._jobs.finish(job.id, status=JOB_CANCELLED,
                                  message=f"已取消（已处理 {done} 个）")
                return self._summarize(job.id)

            if self._posts.is_settled(tid):
                # ★ 只跳过**终态**（done / nolink）。
                #   早先用 ``exists()``（任意状态都跳），配上「会话失效时
                #   只标 pending」就成了一条死路：实测 13 帖匿名采成 pending
                #   后，配好凭据重采**一个都不会变**，而 pending 又不在前端
                #   展示范围内，连手动删掉重来都做不到。
                #
                # ★ 已入库的帖子也要顺带修图：用户可能删过 data/images/，
                #   而帖子本身在库里 → 单看「tid 已存在」会永远跳过它，
                #   本地图路径就永久指向不存在的文件（前端裂图）。
                #   修图只用库里的源 URL，**不碰论坛**，所以不受限速影响，
                #   也不算「重新采集」。
                self._repair_images(tid)
                self._jobs.bump(job.id, processed=1, skipped=1)
                _tick()
                continue

            self._jobs.update(job.id, current_tid=tid)
            try:
                self._collect_one(tid, post_date)
                collected_tids.append(tid)
                self._jobs.bump(job.id, processed=1, collected=1)
                _tick()
            except LoginRequired as exc:
                # 会话失效：交给上层重登录后重试整个任务更安全，
                # 这里先如实记录并中止，避免用坏会话把剩余帖子全刷成失败。
                self._jobs.finish(job.id, status=JOB_FAILED,
                                  message=f"会话失效：{exc}")
                raise
            except (FetchError, Exception) as exc:  # noqa: BLE001
                log.warning("帖子 %s 采集失败：%s", tid, exc)
                self._jobs.bump(job.id, processed=1, failed=1)
                _tick()

        final = self._jobs.get(job.id)
        msg = (f"完成：新增 {final.collected if final else 0} 个，"
               f"跳过 {final.skipped if final else 0} 个，"
               f"失败 {final.failed if final else 0} 个")
        # ★ 先查 Emby 再落终态：同步慢 1～3 秒,这段时间任务还停在
        #   running,前端轮询不会提前拿到 done；一旦 done,徽章必已就位。
        #   若反过来先 finish,前端看到 done 立刻重载,徽章还没写进库,
        #   用户只能等 15 分钟定时器或手动刷新——那这个功能就白做了。
        #   回调异常只记日志：Emby 挂了不该让采集任务显得失败。
        if collected_tids and self.on_job_done is not None:
            try:
                self.on_job_done(collected_tids)
            except Exception:  # noqa: BLE001
                log.warning("job_done 回调异常（已忽略）", exc_info=True)
        self._jobs.finish(job.id, status=JOB_DONE, message=msg)
        return self._summarize(job.id)

    # ---------------------------------------------------------------- 发现

    def _discover(
        self, job: CollectJob, wanted: set[str]
    ) -> tuple[list[tuple[int, str]], int]:
        """翻列表页收集 ``(tid, post_date)``。

        返回 ``(found, pages)``。found **按发现顺序**（= 时间降序）。

        ★ 必须带上**每一帖自己的**日期：早先只返回 tid，调用方拿
        ``job.from_date`` 当归档日期，跨日采集时会把范围里所有帖
        都错分到范围起始那天。
        """
        found: list[tuple[int, str]] = []
        seen: set[int] = set()
        oldest = min(wanted)

        for page in range(1, config.MAX_BACKFILL_PAGES + 1):
            if self._jobs.is_cancel_requested(job.id):
                break

            rows = self._c.list_page(fid=job.fid, page=page)
            self._jobs.bump(job.id, pages=1)
            if not rows:
                break                      # 翻到底了

            page_dates = [r.post_date for r in rows if r.post_date]
            for row in rows:
                if row.post_date and row.post_date in wanted and row.tid not in seen:
                    seen.add(row.tid)
                    found.append((row.tid, row.post_date))

            if not page_dates:
                # 整页都没解析出日期：结构可能变了，继续翻页风险大
                log.warning("第 %s 页无任何日期，停止翻页", page)
                break

            # ★ 提前退出：本页最旧日期已早于范围下界，后续页只会更早
            if min(page_dates) < oldest:
                break

            log.debug("第 %s 页：%s → %s（累计 %s 个）",
                      page, max(page_dates), min(page_dates), len(found))

        return found, page

    # ---------------------------------------------------------------- 单帖

    def _collect_one(self, tid: int, post_date: str) -> None:
        """抓一个帖子并入库。

        若帖子需要「感谢」才能看到 ed2k（实测：匿名或未感谢时 ed2k 隐藏），
        且有可用会话，则**就地解锁**并重新解析——这样一趟采集就能直接
        得到 ``done`` 而不是留下一堆 ``pending``。

        解锁失败**不算采集失败**：帖子其余字段（番号/演员/封面）已拿到，
        先以 ``pending`` 入库，日后重跑可再试。
        """
        detail = self._c.fetch_thread(tid)
        status = "done" if detail.ed2k else ("pending" if detail.locked else "nolink")

        if status == "pending" and self._thanks is not None:
            try:
                # ★ 把刚抓到的 HTML 传进去，省一次 2–5 秒的限速请求
                result = self._thanks.thank(tid, html=detail.html)
            except LoginRequired:
                raise
            except ThanksError as exc:
                log.warning("帖子 %s 感谢失败：%s", tid, exc)
            else:
                # ★ 直接解析感谢流程里已经拿到的页面，不再多发一次请求。
                #   解锁后正文会变（出现 ed2k 与附件块），必须重新解析。
                if result.html:
                    detail = parse_thread_detail(result.html, tid)
                    status = (
                        "done" if detail.ed2k
                        else ("pending" if detail.locked else "nolink")
                    )

        self._posts.upsert_collected(
            tid=tid,
            title=detail.title,
            code=detail.code,
            actress=detail.actress,
            release_date=detail.release_date,
            size=detail.size,
            cover_img=detail.cover_img,
            detail_img=detail.detail_img,
            ed2k=detail.ed2k,
            post_date=post_date,
            status=status,
            **self._localize(detail),
        )

    def _localize(self, detail) -> dict[str, str | None]:  # type: ignore[no-untyped-def]
        """把封面图/详情图本地化，返回要写入的本地路径。

        ★ 每张源图**一次下载**生成全部档位，再分发给各自的列：

        - ``cover_local``  → **600px**：卡片缩略图
        - ``detail_local`` → **1200px**：中间档
        - ``cover_orig`` / ``detail_orig`` → **原图**：灯箱大图

        早先两列都写 600px，结果 1200px 文件白生成（占了一半磁盘），
        而灯箱只能显示 600px 的模糊图——**生成的东西必须有人用**。

        **不抛异常**：图片失败不该让整帖采集失败——番号、演员、ed2k
        比图重要得多。失败时返回空字典，那些列保持原值（见
        ``upsert_collected`` 的 COALESCE 语义）。
        """
        if self._images is None:
            return {}
        cached = self._images.ensure_post(detail)
        out: dict[str, str | None] = {}
        for field, img in cached.items():
            for col, width in FIELD_TARGETS.get(field, ()):
                out[col] = img.variants.get(width)
        return out

    def _repair_images(self, tid: int) -> None:
        """补齐已入库帖子的缺失本地图（不访问论坛）。

        与 ``_localize`` 的区别：这里的数据来自**数据库**（``cover_img``
        源 URL 早就存下来了），所以纯本地操作，零网络请求、零限速开销。

        ★ **本方法绝不抛异常**。它是对「已跳过」帖子的附带修补，而一个
        采集任务可能跑几十分钟；因为某张图的问题让整个任务失败，
        等于用芝麻换西瓜。任何异常都只记日志。
        """
        if self._images is None:
            return
        try:
            post = self._posts.get(tid)
            if post is None:
                return
            for field in ("cover", "detail"):
                url = getattr(post, f"{field}_img", None)
                if not url:
                    continue
                # ★ 一个源图对应多列（缩略图 + 原图），逐列检查。
                #   早先只检查一列，导致「原图档没生成」永远不会被发现。
                cached = None
                for col, want in FIELD_TARGETS[field]:
                    local = getattr(post, col, None)
                    # ★ 三个条件都要看：
                    #   - 文件在不在（磁盘）
                    #   - 路径记没记（数据库）
                    #   - 记的**宽度对不对**（见下）
                    #
                    # 只看 has() 会漏掉「文件在、但 cover_local 是 NULL」的
                    # 情况（例如迁移/手改库），那时前端白白回退到外链。
                    #
                    # 只看「非空 + has()」会漏掉**档位记错**的情况：早先版本
                    # 把 detail_local 也写成 600px，而 1200px 文件本来就在，
                    # has() 返回 True → 永远跳过 → 灯箱一直显示 600px 模糊图。
                    #
                    # ★ 匹配 ``-orig.`` / ``-600.webp`` 而非 ``endswith("orig")``：
                    #   原图档是 ``-orig.jpg``，拿后缀名去比恒为 False，
                    #   会导致每次采集都重下原图。
                    if (local and self._images.has(url)
                            and matches_width(local, want)):
                        continue
                    if cached is None:
                        cached = self._images.ensure(url)
                    self._posts.set_local_image(
                        tid, col, cached.variants[want],
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("帖子 %s 补图失败（不影响采集）：%s", tid, exc)

    # ---------------------------------------------------------------- 汇总

    def _summarize(self, job_id: int) -> CollectResult:
        job = self._jobs.get(job_id)
        assert job is not None
        return CollectResult(
            job_id=job.id,
            status=job.status,
            total=job.total,
            collected=job.collected,
            skipped=job.skipped,
            failed=job.failed,
            pages=job.pages,
            message=job.message,
        )


#: RSS 轮询间隔。需求 C-1 建议 5–10 分钟；20 条窗口 ÷ 41 帖/天峰值
#: → 5 分钟绰绰有余（每天 288 次 × 20 = 5760 条容量 vs 41 帖需求）。
POLL_INTERVAL_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class RssPollResult:
    """一次 RSS 轮询的结果。

    ``checked`` / ``new`` / ``collected`` 三个数字刻意分开：

    - ``checked``：feed 里有几个可用条目（含已入库的）
    - ``new``：其中几个是库里没有的
    - ``collected``：实际成功入库几个

    合并成一个数字会看不出「feed 读到了但没采」与「feed 没读到」的差别，
    而这两种情况的排查方向完全不同。
    """

    checked: int = 0
    new: int = 0
    collected: int = 0
    skipped: int = 0
    failed: int = 0
    job_id: int | None = None
    error: str | None = None


@dataclass
class RssPoller:
    """定时轮询 RSS 发现新帖（C-1）。

    ★ **绝不抛异常**：这是后台定时任务。网络挂了、被 WAF 拦了、feed 结构
    变了——全部吞掉记在 :class:`RssPollResult` 里。异常逃出去只会静默
    杀死轮询线程，用户从此再也发现不了新帖。

    ★ **不与在跑的任务抢**：采集并发度恒为 1（封号风险硬约束）。
    已有任务在跑时本轮只「发现」，不「采集」。

    设计：RSS 只用于**发现**（tid + 发帖日期）。ed2k 在 ``<description>``
    里被截断掉了（事实 #2），必须逐帖抓详情页——那是 :class:`Collector`
    的活，这里不重复实现。
    """

    collector: Collector
    posts: PostRepo
    jobs: CollectRepo
    feed: Any                     # 需有 .get(url) → 带 .text 的对象
    feed_url: str
    fid: str = config.DEFAULT_FID
    #: 采集通道。给了就走它的锁（与手动采集真正互斥）。
    runner: CollectRunner | None = None

    def tick(self, *, claim: bool = True) -> RssPollResult:
        """跑一轮。任何失败都记在返回值里，不抛。

        Args:
            claim: 是否自己去抢采集通道。调用方**已经持锁**时传 ``False``，
                否则会发现自己被占着，本轮直接空转。
        """
        try:
            xml = self._fetch()
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS 拉取失败：%s", exc)
            return RssPollResult(error=str(exc))

        items = parse_rss(xml)
        if not items:
            # ★ 空 feed 不是错误。被 WAF 拦时返回的 HTML 也走这条路。
            #   注意不能因此认为「没有新帖」之外还发生了什么。
            return RssPollResult(checked=0)

        fresh = [(i.tid, i.post_date) for i in items
                 if not self.posts.is_settled(i.tid)]
        result = RssPollResult(checked=len(items), new=len(fresh))
        if not fresh:
            return result

        # ★ 占住采集通道。并发度恒为 1 是封号风险的硬约束。
        if claim and not self._claim():
            log.info("采集通道被占用，本轮 RSS 只发现不采集（%s 个新帖）",
                     len(fresh))
            return result

        job = None
        try:
            dates = [d for _, d in fresh]
            job = self.jobs.create(
                from_date=min(dates), to_date=max(dates), fid=self.fid)
            # ★ 必须写 total：它平时由 `_run_job`（列表页路径）设置，
            #   而 RSS 直连 `fetch_items`。不补上则 `percent` 永远是 0，
            #   前端进度条一直空着（`total <= 0` 时 percent 只在终态返回 100）。
            self.jobs.update(job.id, phase="fetching", total=len(fresh))
            out = self.collector.fetch_items(job, fresh)
        except Exception as exc:  # noqa: BLE001
            log.warning("RSS 轮询采集失败：%s", exc)
            return RssPollResult(
                checked=result.checked, new=result.new,
                job_id=job.id if job else None, error=str(exc))
        finally:
            if claim and self.runner is not None:
                self.runner.end()

        return RssPollResult(
            checked=result.checked, new=result.new,
            collected=out.collected, skipped=out.skipped, failed=out.failed,
            job_id=out.job_id,
        )

    def _claim(self) -> bool:
        """尝试占住采集通道。

        有 ``runner`` 时用它的锁（与手动采集真互斥）；没给时退化为
        看数据库里有没有在跑的任务——单测常用这种轻量形式。
        """
        if self.runner is not None:
            return self.runner.try_begin()
        return self.jobs.active() is None

    def _fetch(self) -> str:
        page = self.feed.get(self.feed_url)
        return page.text


class CollectRunner:
    """把采集放到后台线程，并提供单飞保证。

    HTTP 请求不能等数十分钟，因此 ``POST /api/collect`` 立即返回 job_id，
    前端轮询 ``/api/collect/status``。
    """

    def __init__(self, collector: Collector, jobs: CollectRepo) -> None:
        self._collector = collector
        self._jobs = jobs
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: 手动占用的通道（RSS 轮询用）。见 :meth:`try_begin`。
        self._busy = False

    @property
    def running(self) -> bool:
        """是否有人占着采集通道（后台线程或手动占位）。"""
        with self._lock:
            if self._busy:
                return True
            return self._thread is not None and self._thread.is_alive()

    def try_begin(self) -> bool:
        """非阻塞地占住采集通道，成功返回 True。

        ★ 给 :class:`RssPoller` 用。它必须在**同一把锁**下与手动采集互斥，
        不能只看 ``jobs.active()``：那是数据库层面的检查，与线程状态之间
        有一个窗口——查完之后、建任务之前，手动采集可能刚好启动，
        结果两个采集同时在跑。而「采集并发度恒为 1」是封号风险的硬约束。

        拿到后必须用 :meth:`end` 释放。
        """
        with self._lock:
            if self._busy:
                return False
            if self._thread is not None and self._thread.is_alive():
                return False
            self._busy = True
            return True

    def end(self) -> None:
        """释放 :meth:`try_begin` 占的通道。可重复调用。"""
        with self._lock:
            self._busy = False

    def start(self, *, from_date: str, to_date: str,
              fid: str = config.DEFAULT_FID) -> CollectJob:
        """启动后台采集。

        Raises:
            ValidateError: 日期非法（**在起线程前**校验，以便同步返回 400）。
            CollectError: 已有任务在跑。
        """
        date_range(from_date, to_date)          # 先校验，失败不建任务

        with self._lock:
            if self._busy:
                raise CollectError("已有采集任务正在运行")
            if self._thread is not None and self._thread.is_alive():
                raise CollectError("已有采集任务正在运行")
            # 进程重启后残留的 running 任务会挡住 create
            self._jobs.reap_orphans()
            job = self._jobs.create(from_date=from_date, to_date=to_date, fid=fid)

            def _worker() -> None:
                try:
                    self._collector.run(
                        from_date=from_date, to_date=to_date, fid=fid, job=job
                    )
                except Exception:  # noqa: BLE001 —— 已在 run 内落库
                    log.exception("后台采集线程异常")

            self._thread = threading.Thread(
                target=_worker, name=f"collect-{job.id}", daemon=True
            )
            self._thread.start()
            return job

    def start_poll(self, work: Callable[[bool], Any]) -> None:
        """在**已持锁**的前提下起一个后台线程跑 ``work(claim)``。

        给「手动触发 RSS 轮询」接口用。它能**同步**地判断通道是否空闲：
        先 :meth:`try_begin`，拿不到就立刻抛 :class:`CollectError`（转 409），
        而不是起了线程才在后台默默失败。拿到后把锁转交给后台线程，
        由它跑完释放。

        ``work`` 收到 ``claim=False``：告诉它通道已经被本方法占住了。
        """
        with self._lock:
            if self._busy:
                raise CollectError("已有采集任务正在运行")
            if self._thread is not None and self._thread.is_alive():
                raise CollectError("已有采集任务正在运行")
            self._busy = True

            def _worker() -> None:
                try:
                    work(False)
                except Exception:  # noqa: BLE001
                    log.exception("轮询线程异常")
                finally:
                    self.end()

            self._thread = threading.Thread(
                target=_worker, name="rss-poll-once", daemon=True)
            self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        """等待后台线程结束（测试用）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def request_cancel(self, job_id: int) -> bool:
        return self._jobs.request_cancel(job_id)
