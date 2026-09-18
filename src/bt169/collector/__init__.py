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
from bt169.source.parse import parse_thread_detail
from bt169.source.thanks import ThanksClient, ThanksError

from bt169.collector.imagecache import (
    CARD_WIDTH,
    LIGHTBOX_WIDTH,
    ImageCache,
    ImageError,
)

__all__ = [
    "Collector",
    "CollectResult",
    "CollectError",
    "date_range",
    "ValidateError",
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


#: 每个字段该用哪一档宽度。两处写入（新采 + 补图）共用，避免漂移。
WIDTH_FOR_FIELD: dict[str, int] = {
    "cover": CARD_WIDTH,        # 卡片缩略图
    "detail": LIGHTBOX_WIDTH,   # 灯箱大图
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
        tids, pages = self._discover(job, wanted)

        self._jobs.update(
            job.id, phase="fetching", total=len(tids), pages=pages,
            message=f"发现 {len(tids)} 个帖子，开始抓取",
        )

        # ---------------- 阶段 2：抓取

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

        for tid in tids:
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
                self._collect_one(tid, job.from_date)
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
        self._jobs.finish(
            job.id, status=JOB_DONE,
            message=f"完成：新增 {final.collected if final else 0} 个，"
                    f"跳过 {final.skipped if final else 0} 个，"
                    f"失败 {final.failed if final else 0} 个",
        )
        return self._summarize(job.id)

    # ---------------------------------------------------------------- 发现

    def _discover(self, job: CollectJob, wanted: set[str]) -> tuple[list[int], int]:
        """翻列表页收集 tid。

        返回 ``(tids, pages)``。tids **按发现顺序**（= 时间降序）。
        """
        tids: list[int] = []
        seen: set[int] = set()
        newest = max(wanted)
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
                    tids.append(row.tid)

            if not page_dates:
                # 整页都没解析出日期：结构可能变了，继续翻页风险大
                log.warning("第 %s 页无任何日期，停止翻页", page)
                break

            # ★ 提前退出：本页最旧日期已早于范围下界，后续页只会更早
            if min(page_dates) < oldest:
                break
            # 本页最新日期都已早于下界（理论上不会发生，防御性）
            if max(page_dates) < oldest:
                break

            log.debug("第 %s 页：%s → %s（累计 %s 个）",
                      page, max(page_dates), min(page_dates), len(tids))

        return tids, page

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

        ★ 两档宽度各有用途（`FRONTEND.md` §8.3 / §531）：

        - ``cover_local`` → **600px**：卡片缩略图
        - ``detail_local`` → **1200px**：灯箱大图

        早先两列都写 600px，结果 1200px 文件白生成（占了一半磁盘），
        而灯箱只能显示 600px 的模糊图——**生成的东西必须有人用**。

        **不抛异常**：图片失败不该让整帖采集失败——番号、演员、ed2k
        比图重要得多。失败时返回空字典，那些列保持原值（见
        ``upsert_collected`` 的 COALESCE 语义）。
        """
        if self._images is None:
            return {}
        cached = self._images.ensure_post(detail)
        return {
            f"{field}_local": img.variants.get(WIDTH_FOR_FIELD[field])
            for field, img in cached.items()
            if field in WIDTH_FOR_FIELD
        }

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
                local = getattr(post, f"{field}_local", None)
                if not url:
                    continue
                want = WIDTH_FOR_FIELD[field]
                # ★ 三个条件都要看：
                #   - 文件在不在（磁盘）
                #   - 路径记没记（数据库）
                #   - 记的**宽度对不对**（见下）
                #
                # 只看 has() 会漏掉「文件在、但 cover_local 是 NULL」的情况
                # （例如迁移/手改库），那时前端白白回退到外链。
                #
                # 只看「非空 + has()」会漏掉**宽度记错**的情况：早先版本
                # 把 detail_local 也写成 600px，而 1200px 文件本来就在，
                # has() 返回 True → 永远跳过 → 灯箱一直显示 600px 模糊图。
                if local and self._images.has(url) and local.endswith(
                    f"-{want}.webp"
                ):
                    continue
                self._images.ensure(url)
                self._posts.set_local_image(tid, field, self._images.url_for(
                    self._images.key_for(url), want,
                ))
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

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, *, from_date: str, to_date: str,
              fid: str = config.DEFAULT_FID) -> CollectJob:
        """启动后台采集。

        Raises:
            ValidateError: 日期非法（**在起线程前**校验，以便同步返回 400）。
            CollectError: 已有任务在跑。
        """
        date_range(from_date, to_date)          # 先校验，失败不建任务

        with self._lock:
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

    def join(self, timeout: float | None = None) -> None:
        """等待后台线程结束（测试用）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def request_cancel(self, job_id: int) -> bool:
        return self._jobs.request_cancel(job_id)
