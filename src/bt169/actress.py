"""LTX 演员回填：把 javdb 的演员名逐条补进本地镜像（L-12）。

**为什么要有这个模块**：AVdb 的 LTX 资源对象里**没有演员字段**，演员只能
从它的 javdb 模块取（``detail_url`` 末尾就是 movie id）。同步一次只要几秒，
而 1450 条资源的演员要问上游 1450 次、每条 0.6–14 秒——放进同步请求里，
用户得开着页面等一小时。所以拆成两件事：

1. ``POST /api/ltx/sync`` 仍然只做「拉列表、写镜像」，几秒返回；
2. 同步成功后 :meth:`ActressBackfiller.start` 起一个**后台**线程，串行地
   把还没查过的行补齐，边查边落库。补齐之后卡片和灯箱都直接读库
   （``ltx_items.actress``），零上游请求。

**三条硬约束（都是实测出来的，不是猜的）**：

- **必须串行**。6 路并发打 javdb 详情 → 40/40 全 502；串行 +0.5 秒间隔
  → 全成功。所以这里既不排队也不并发，就是「一条一条来」，
  并且和按需查询共用 :data:`GATE` 这把闸门。
- **新的日期优先**。用户打开 LTX 页看的是最近几天，回填按
  ``publish_date DESC`` 走，那几天几十秒内就有；从三年前开始补的话，
  用户会盯着一整屏空白。
- **失败要能退**。上游挂了 / 账号被限流时，不能每 0.5 秒重试一条永不成功
  的请求（那等于自己把账号打死）。连续失败 :data:`MAX_FAILS` 条就结束
  本轮，等下一次同步或用户手动触发。

**失败与「没有」必须分开**（否则会永远重查同一批行）：

- 查到演员 → 记 ``actress`` + ``actress_at``；
- 上游明确「这条没有」/「编号对不上」→ **只记** ``actress_at``；
- 上游报错（超时、502、鉴权）→ **什么都不记**，留待下一轮。

**只跑在服务端**：线程是 daemon，``stop()`` 用 ``Event.wait`` 打断间隔睡眠，
所以在间隔里被叫停是立刻退出的（正在等上游的那一条会等它超时）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from bt169.avdb import AvdbError
from bt169.models import LtxItem
from bt169.repo.posts import now_iso

__all__ = [
    "GATE",
    "GATE_WAIT",
    "MAX_FAILS",
    "TickResult",
    "ActressBackfiller",
]

log = logging.getLogger(__name__)

#: 上游串行闸门。回填线程与 ``GET /api/ltx/{id}/actress`` 共用一把：
#: 两条路径同时打 javdb 就是「并发」，实测直接 502。
GATE = threading.Lock()

#: 抢不到闸门时最多等多久（秒）。灯箱一次只开一个，2 秒够正常使用；
#: 回填正在等上游时，按需查询最多等这么久，等不到就回 ``busy``。
GATE_WAIT = 2.0

#: 一轮回填里连续失败多少条就收手。上游限流 / 账号被锁时，
#: 继续一条条重试只会让情况更糟，也会把日志刷满。
MAX_FAILS = 5


class _Source(Protocol):
    """回填只需要数据源的一个能力（便于测试注入假对象）。"""

    def lookup_actress(
        self, *, detail_url: str | None = None, number: str | None = None
    ) -> tuple[str | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class TickResult:
    """一次 ``tick`` 的结果（返回值，不抛异常）。"""

    #: 有结论的条数（查到 + 确认没有）
    asked: int = 0
    #: 其中查到了演员的条数
    found: int = 0
    #: 其中确认「上游没有」的条数
    empty: int = 0
    #: 上游失败的条数（这些行**不记** ``actress_at``，留待下次）
    failed: int = 0
    #: 本次之后还剩多少条待查
    remaining: int = 0


class ActressBackfiller:
    """把「还没查过演员」的行一批批补齐（后台线程 + 串行上游）。

    Args:
        build_source: 每次 tick **重建**数据源——用户在设置页改了 AVdb
            地址/账号后无需重启服务。返回 ``None`` 表示未配置。
        ltx: 镜像表仓库（读待办、写结果）。
        batch: 单次 tick 最多查多少条。太小则每轮都要重建数据源，
            太大则一轮跑十几分钟才检查一次停止标志。
        gap: 条与条之间的间隔（秒）。实测 0.5 秒串行不会触发 javdb 限流。
        clock: 可注入的时间源（写 ``actress_at``）。
    """

    def __init__(
        self,
        *,
        build_source: Any,
        ltx: Any,
        batch: int = 25,
        gap: float = 0.5,
        clock: Any = None,
    ) -> None:
        self._build_source = build_source
        self._ltx = ltx
        self._batch = max(1, int(batch))
        self._gap = max(0.0, float(gap))
        self._clock = clock or now_iso
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: 上一次 tick 的摘要（排查用；状态接口读的是库里的计数）。
        self.last: TickResult | None = None

    # ------------------------------------------------------------ 状态

    @property
    def running(self) -> bool:
        """回填线程是否在跑。"""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 干一次

    def tick(self) -> TickResult:
        """处理一批待查的行。**绝不抛异常**（后台线程的最后一道防线）。"""
        try:
            pending = self._ltx.pending_actress(self._batch)
        except Exception as exc:  # noqa: BLE001 — 库坏了也不能杀死线程
            log.warning("LTX 演员回填：读待办失败：%s", exc)
            return TickResult()

        if not pending:
            self.last = TickResult()
            return self.last

        try:
            source = self._build_source()
        except Exception as exc:  # noqa: BLE001
            log.warning("LTX 演员回填：数据源不可用：%s", exc)
            return TickResult(remaining=len(pending))
        if source is None:                       # 没配置 → 静默跳过
            return TickResult(remaining=len(pending))

        asked = found = empty = failed = 0
        for item in pending:
            if self._stop.is_set():
                break
            ok, names = self._ask(source, item)
            if not ok:                           # 这次没问到 → 不记账
                failed += 1
                if failed >= MAX_FAILS:
                    log.warning(
                        "LTX 演员回填：连续 %s 条失败，本轮收手（下次同步再试）",
                        failed,
                    )
                    break
                self._pause()
                continue

            asked += 1
            self._record(item, names)
            if names:
                found += 1
            else:
                empty += 1
            self._pause()

        self.last = TickResult(
            asked=asked, found=found, empty=empty, failed=failed,
            remaining=self._remaining(),
        )
        if asked or failed:
            log.info(
                "LTX 演员回填：本轮 %s 条（查到 %s / 没有 %s / 失败 %s），"
                "还剩 %s 条",
                asked, found, empty, failed, self.last.remaining,
            )
        return self.last

    # ------------------------------------------------------------ 线程

    def start(self) -> bool:
        """启动回填线程（幂等）。返回是否真的启动了新线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._drain, name="ltx-actress", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        """叫停回填（幂等）。正在等上游的那一条会等它超时。"""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _drain(self) -> None:
        """一直补到没有待办 / 上游在报错 / 被叫停为止。"""
        while not self._stop.is_set():
            result = self.tick()
            # 一条都没问出结论 → 要么查完了，要么上游/配置有问题。两种都该
            # 收手：继续转下去只会每 0.5 秒往一个坏掉的上游再敲一次。
            if not result.asked:
                return
            # 有进展 → 立刻下一批（条间隔已经在 ``tick`` 里付过了）

    # ------------------------------------------------------------ 内部

    def _ask(self, source: _Source, item: LtxItem) -> tuple[bool, str | None]:
        """问一条。返回 ``(这次问到没有, 演员串)``。

        ★ 第二个位置的 ``None`` 表示**上游明确说没有**（要记 ``actress_at``），
          而不是「没问到」——两者搞混的话，查不到的行会被无限重查。
        """
        if not GATE.acquire(timeout=GATE_WAIT):
            # 闸门被按需查询长期占着 → 当作没问到，下一轮再试。
            log.info("LTX 演员回填：闸门忙，暂缓 %s", item.number)
            return False, None
        try:
            names, movie_id = source.lookup_actress(
                detail_url=item.detail_url, number=item.number
            )
        except AvdbError as exc:
            log.info("LTX 演员回填：%s 查询失败：%s", item.number, exc)
            return False, None
        except Exception as exc:  # noqa: BLE001 — 后台线程绝不外抛
            log.warning("LTX 演员回填：%s 意外错误：%s", item.number, exc)
            return False, None
        finally:
            GATE.release()

        if names:
            log.info("LTX 演员回填：%s → %s（javdb %s）", item.number, names, movie_id)
            return True, names
        log.info(
            "LTX 演员回填：%s 上游无演员（javdb %s）", item.number, movie_id or "—"
        )
        return True, None

    def _record(self, item: LtxItem, names: str | None) -> None:
        """落库。写失败只记日志——下一轮会重问一次，不算灾难。"""
        try:
            self._ltx.set_actress(item.id, names, self._clock())
        except Exception as exc:  # noqa: BLE001
            log.warning("LTX 演员回填：写入 %s 失败：%s", item.id, exc)

    def _remaining(self) -> int:
        try:
            return self._ltx.actress_stats()[1]
        except Exception:  # noqa: BLE001
            return 0

    def _pause(self) -> None:
        """条间隔：用 ``Event.wait`` 而不是 ``sleep``，被叫停时立刻返回。"""
        if self._gap > 0:
            self._stop.wait(self._gap)
