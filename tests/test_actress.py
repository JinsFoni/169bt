"""LTX 演员回填（``bt169/actress.py``，L-12）。

★ 这里全部**离线**：javdb 的查询用假数据源，时间用假时钟，条间隔压到 0。
  线程只在最后两条测试里真的起一次（用空待办，瞬间退出），其余直接调
  ``tick()``——起真线程跑 `sleep` 的测试会又慢又闪。
"""

from __future__ import annotations

import threading

import pytest

from bt169 import actress as actress_mod
from bt169.actress import ActressBackfiller
from bt169.avdb import AvdbError, AvdbNotConfigured
from bt169.models import LtxItem
from bt169.repo.ltx import LtxRepo


# ---------------------------------------------------------------- 替身


def item(tid, *, number="SSIS-157", date="2026-10-25", detail="https://javdb.com/v/mk01"):
    return LtxItem(
        id=tid, number=number, title=f"标题 {tid}", section="LTX破解",
        publish_date=date, size_bytes=None, ed2k=None, cover_url=None,
        detail_url=detail, rejected=0, created_at=None, tg_sent_at=None,
        fetched_at="2026-10-25T00:00:00+08:00",
    )


class FakeRepo:
    """只实现回填需要的三个方法；``rows`` 就是待办队列。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.writes = []          # (id, actress, when)
        self.limits = []
        self.write_exc = None

    def pending_actress(self, limit):
        self.limits.append(limit)
        return self.rows[:limit]

    def set_actress(self, item_id, actress, when):
        if self.write_exc is not None:
            raise self.write_exc
        self.writes.append((item_id, actress, when))
        self.rows = [r for r in self.rows if r.id != item_id]

    def actress_stats(self):
        return (len(self.writes), len(self.rows))


class FakeSource:
    """假数据源：``answers`` 里按 seq 依次返回结果/异常。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()

    def lookup_actress(self, *, detail_url=None, number=None):
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            self.calls.append((detail_url, number))
            answer = self.answers.pop(0) if self.answers else ("名前", "mk01")
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            with self._lock:
                self.inflight -= 1


def build(repo, source, *, batch=25, clock=None):
    return ActressBackfiller(
        build_source=lambda: source, ltx=repo, batch=batch, gap=0,
        clock=clock or (lambda: "2026-10-28T12:00:00+08:00"),
    )


# ---------------------------------------------------------------- 一次 tick


def test_tick_records_found_and_missing():
    repo = FakeRepo([item(1), item(2)])
    source = FakeSource(("三上悠亜", "mk01"), (None, "mk02"))
    bf = build(repo, source)

    result = bf.tick()

    assert (result.asked, result.found, result.empty, result.failed) == (2, 1, 1, 0)
    assert result.remaining == 0
    # ★ 查不到也要记账（actress 为空但时间写上），否则这条会被永远重查
    assert repo.writes == [
        (1, "三上悠亜", "2026-10-28T12:00:00+08:00"),
        (2, None, "2026-10-28T12:00:00+08:00"),
    ]
    assert source.calls == [("https://javdb.com/v/mk01", "SSIS-157")] * 2


def test_tick_passes_detail_url_and_number():
    repo = FakeRepo([item(1, number="SNOS-437", detail="https://javdb.com/v/822NKW")])
    source = FakeSource(("歌野こころ、足立友梨", "822NKW"))
    build(repo, source).tick()
    assert source.calls == [("https://javdb.com/v/822NKW", "SNOS-437")]
    assert repo.writes[0][1] == "歌野こころ、足立友梨"


def test_tick_upstream_error_is_not_recorded():
    """★ 上游失败**不能**落库：否则这条永久显示「没有演员」。"""
    repo = FakeRepo([item(1)])
    source = FakeSource(AvdbError("AVdb 返回 HTTP 502"))
    result = build(repo, source).tick()

    assert result.failed == 1
    assert result.asked == 0
    assert repo.writes == []
    assert result.remaining == 1


def test_tick_stops_after_repeated_failures():
    """连续失败到 MAX_FAILS 就收手——别拿自己的 javdb 账号硬敲。"""
    repo = FakeRepo([item(i) for i in range(1, 11)])
    source = FakeSource(*[AvdbError("502")] * 10)
    result = build(repo, source).tick()

    assert result.failed == actress_mod.MAX_FAILS
    assert len(source.calls) == actress_mod.MAX_FAILS
    assert repo.writes == []


def test_tick_stops_at_batch_size():
    repo = FakeRepo([item(i) for i in range(1, 51)])
    source = FakeSource()
    result = build(repo, source, batch=3).tick()

    assert result.asked == 3
    assert repo.limits == [3]
    assert result.remaining == 47


def test_tick_is_serial_one_request_at_a_time():
    """★ 6 路并发打 javdb 会 40/40 全 502，所以这里钉住「同时只有一条在飞」。"""
    repo = FakeRepo([item(i) for i in range(1, 6)])
    source = FakeSource()
    build(repo, source).tick()
    assert source.max_inflight == 1


def test_tick_with_empty_queue_does_not_call_upstream():
    repo = FakeRepo([])
    source = FakeSource()
    result = build(repo, source).tick()
    assert result == actress_mod.TickResult()
    assert source.calls == []


def test_tick_without_configured_source_does_nothing():
    """没填 AVdb 账号：``build_source`` 返回的数据源一用就抛，静默跳过。"""
    repo = FakeRepo([item(1)])

    class Unconfigured:
        def lookup_actress(self, *, detail_url=None, number=None):
            raise AvdbNotConfigured("未配置 LTX 数据源")

    result = build(repo, Unconfigured()).tick()
    assert result.asked == 0
    assert result.failed == 1
    assert repo.writes == []


def test_tick_survives_write_failure():
    repo = FakeRepo([item(1)])
    repo.write_exc = RuntimeError("database is locked")
    result = build(repo, FakeSource()).tick()
    # 写了但失败：仍算「问到了」，只是库没落上（下轮会重问，不算灾难）
    assert result.asked == 1
    assert repo.writes == []


def test_tick_when_gate_is_busy_skips_without_upstream(monkeypatch):
    repo = FakeRepo([item(1)])
    source = FakeSource()
    monkeypatch.setattr(actress_mod, "GATE_WAIT", 0.01)
    actress_mod.GATE.acquire()
    try:
        result = build(repo, source).tick()
    finally:
        actress_mod.GATE.release()

    assert result.failed == 1
    assert source.calls == []


def test_tick_never_raises_when_repo_is_broken():
    class Broken:
        def pending_actress(self, limit):
            raise RuntimeError("db gone")

    result = ActressBackfiller(build_source=lambda: None, ltx=Broken()).tick()
    assert result == actress_mod.TickResult()


# ---------------------------------------------------------------- 线程


def test_start_is_idempotent_and_stop_is_clean():
    """★ 幂等：多个标签页同时叫回填时不能起两个线程（会各自打上游）。"""
    release = threading.Event()

    class SlowSource:
        def lookup_actress(self, *, detail_url=None, number=None):
            release.wait(5)
            return ("名前", "mk01")

    repo = FakeRepo([item(1)])
    bf = build(repo, SlowSource())

    try:
        assert bf.start() is True
        assert bf.start() is False          # 已在跑
        assert bf.running is True
    finally:
        release.set()
        bf.stop()
    assert bf.running is False
    assert repo.writes and repo.writes[0][1] == "名前"


def test_stop_interrupts_between_items():
    """叫停后不再继续问下一条（正在等上游的那条会等它超时）。"""
    repo = FakeRepo([item(1), item(2), item(3)])
    bf = build(repo, FakeSource())
    bf._stop.set()
    result = bf.tick()
    assert result.asked == 0
    assert repo.writes == []


def test_drain_fills_everything_then_exits():
    repo = FakeRepo([item(i) for i in range(1, 31)])
    source = FakeSource(*[("名前", "mk01")] * 30)
    bf = build(repo, source, batch=10)

    bf._drain()                  # 同步调用（不额外起线程）

    assert len(repo.writes) == 30
    assert source.max_inflight == 1
    assert repo.rows == []


def test_drain_stops_when_no_progress():
    """上游全挂 → 一轮没进展就退出，不会自己转成忙等。"""
    repo = FakeRepo([item(1)])
    source = FakeSource(AvdbError("502"))
    bf = build(repo, source)

    bf._drain()

    # 待办只剩 1 条 → 这一轮 failed=1、asked=0 → 收手（下次同步再试）。
    # 批量里更长的连续失败由 MAX_FAILS 兜住，见上一条测试。
    assert len(source.calls) == 1
    assert repo.writes == []


@pytest.mark.parametrize("gap", [0, 0.01])
def test_pause_is_interruptible(gap):
    """条间隔用 ``Event.wait``：叫停时立刻返回，不必等满间隔。"""
    bf = ActressBackfiller(build_source=lambda: None, ltx=FakeRepo(), gap=gap)
    bf._stop.set()
    bf._pause()                  # 不阻塞（stop 已置位）


# ---------------------------------------------------------------- 真库联调


def test_backfill_end_to_end_on_real_repo(db):
    """真 SQLite + 假上游：待办清零、演员落库、重复跑不再问上游。"""
    repo = LtxRepo(db)
    repo.upsert_many([
        dict(id=1, number="SNOS-437", title="t", section="LTX破解",
             publish_date="2026-10-25", size_bytes=None, ed2k=None,
             cover_url=None, detail_url="https://javdb.com/v/822NKW",
             rejected=0, created_at=None),
        dict(id=2, number="SDNM-061", title="t", section="LTX破解",
             publish_date="2026-10-24", size_bytes=None, ed2k=None,
             cover_url=None, detail_url=None, rejected=0, created_at=None),
    ], "2026-10-25T00:00:00+08:00")

    source = FakeSource(("歌野こころ、足立友梨", "822NKW"), (None, None))
    bf = ActressBackfiller(build_source=lambda: source, ltx=repo, batch=10, gap=0)
    bf._drain()

    assert repo.actress_stats() == (2, 0)
    assert repo.get(1).actress == "歌野こころ、足立友梨"
    assert repo.get(2).actress is None and repo.get(2).actress_at

    # 再跑一轮：没有待办了，一次上游都不该打
    before = len(source.calls)
    bf._drain()
    assert len(source.calls) == before


def test_pending_order_is_newest_first(db):
    """用户在看最近几天，回填就得从最近几天开始填。"""
    repo = LtxRepo(db)
    repo.upsert_many([
        dict(id=1, number="A-1", title="t", section=None, publish_date="2020-01-01",
             size_bytes=None, ed2k=None, cover_url=None, detail_url=None,
             rejected=0, created_at=None),
        dict(id=2, number="B-2", title="t", section=None, publish_date="2026-10-25",
             size_bytes=None, ed2k=None, cover_url=None, detail_url=None,
             rejected=0, created_at=None),
    ], "2026-10-25T00:00:00+08:00")

    assert [i.id for i in repo.pending_actress(10)] == [2, 1]


# ---------------------------------------------------------------- 装配


def test_app_mounts_backfiller_but_does_not_autostart(db, box):
    """生产装配：回填器挂进 ``app.state``，但**不自己开跑**。

    ★ 回填要串行打 1400+ 次上游、跑半小时，不该由「服务重启」触发——
      只由「同步完」和「前端叫一声」触发。
    """
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None, image_dir=None, emby_interval=9999)
    with TestClient(app):
        bf = app.state.actress_backfiller
        assert isinstance(bf, ActressBackfiller)
        assert bf.running is False
        # 装配了就能被接口叫醒（这里只验证对象存在且可调用）
        assert hasattr(bf, "start")
    # 退出 lifespan 后线程已被叫停（没有线程在跑就更没事）
    assert app.state.actress_backfiller.running is False


def test_app_without_schedulers_has_no_backfiller(db, box):
    """单测/精简装配（interval=0）→ 没有回填器，接口层当作「能力不存在」。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    app = create_app(db, box=box, ui_dir=None, image_dir=None)
    with TestClient(app):
        assert app.state.actress_backfiller is None
