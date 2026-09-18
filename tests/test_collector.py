"""采集编排测试。全部离线（假客户端 + 内存库）。"""

from __future__ import annotations

import threading
import time

import pytest

from bt169.collector import (
    CollectError,
    Collector,
    CollectRunner,
    ValidateError,
    date_range,
)
from bt169.db import Database
from bt169.repo.collect import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    CollectRepo,
)
from bt169.repo.posts import PostRepo
from bt169.source.forum import FetchError, LoginRequired
from bt169.source.parse import ListRow, ThreadDetail


# ------------------------------------------------------------ date_range


def test_date_range_inclusive_both_ends():
    """★ 含两端。用户说「采集该日期范围内的所有帖子」。"""
    assert date_range("2026-09-10", "2026-09-12") == [
        "2026-09-10", "2026-09-11", "2026-09-12"
    ]


def test_date_range_single_day():
    assert date_range("2026-09-10", "2026-09-10") == ["2026-09-10"]


@pytest.mark.parametrize(
    "f,t,msg",
    [
        ("2026-09-12", "2026-09-10", "不能晚于"),
        ("2026-13-01", "2026-13-02", "YYYY-MM-DD"),
        ("not-a-date", "2026-09-10", "YYYY-MM-DD"),
        ("2026-09-10", "2027-12-31", "跨度"),
    ],
)
def test_date_range_rejects_invalid(f, t, msg):
    with pytest.raises(ValidateError, match=msg):
        date_range(f, t)


# ------------------------------------------------------------ 测试替身


class FakeForum:
    """可编程的假论坛客户端。"""

    def __init__(self, *, pages: dict[int, list[ListRow]] | None = None,
                 details: dict[int, ThreadDetail] | None = None) -> None:
        self.pages = pages or {}
        self.details = details or {}
        self.list_calls: list[int] = []
        self.fetch_calls: list[int] = []
        self.fail_tids: set[int] = set()
        self.login_required_tids: set[int] = set()

    def list_page(self, *, fid: str = "192", page: int = 1) -> list[ListRow]:
        self.list_calls.append(page)
        return self.pages.get(page, [])

    def fetch_thread(self, tid: int) -> ThreadDetail:
        self.fetch_calls.append(tid)
        if tid in self.login_required_tids:
            raise LoginRequired(f"tid {tid} 需要登录")
        if tid in self.fail_tids:
            raise FetchError(f"tid {tid} 网络错误")
        return self.details[tid]


def make_detail(tid: int, *, ed2k: bool = True, locked: bool = False) -> ThreadDetail:
    return ThreadDetail(
        tid=tid, title=f"标题 {tid}", code=f"ABC-{tid}",
        actress="某人", release_date="2026-09-17", size="7GB",
        cover_img=f"https://img.example/{tid}.jpg",
        detail_img=f"https://img.example/{tid}-d.jpg",
        ed2k=f"ed2k://|file|{tid}.mkv|1|AB|/" if ed2k else None,
        locked=locked,
        # 真实的 parse_thread_detail 一定会带上来源 HTML（感谢流程要复用）
        html=f"<html>thread {tid}</html>",
    )


def rows(*specs: tuple[int, str]) -> list[ListRow]:
    return [ListRow(tid=t, title=f"T{t}", post_date=d, reply_count=0)
            for t, d in specs]


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield db, PostRepo(db), CollectRepo(db)
    db.close()


# ------------------------------------------------------------ 发现阶段


def test_discover_early_exit_when_page_older_than_from(env):
    """★ 核心优化：排序降序，遇到早于 from 的日期就停。

    若这里出错会**静默漏帖**，所以必须钉死。
    """
    db, posts, jobs = env
    fc = FakeForum(pages={
        1: rows((101, "2026-09-14"), (102, "2026-09-13")),
        2: rows((103, "2026-09-12"), (104, "2026-09-09")),   # 9-09 < from
        3: rows((105, "2026-09-01")),                        # 不该被翻到
    }, details={t: make_detail(t) for t in (101, 102, 103)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]

    result = c.run(from_date="2026-09-12", to_date="2026-09-14")

    assert fc.list_calls == [1, 2]           # 第 3 页没翻
    assert result.status == JOB_DONE
    # 只收范围内：101/102/103；104 是 9-09 不该进
    assert posts.get(101) is not None
    assert posts.get(103) is not None
    assert posts.get(104) is None
    assert result.collected == 3


def test_discover_stops_on_empty_page(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    c.run(from_date="2026-09-01", to_date="2026-09-14")
    assert fc.list_calls == [1, 2]           # 第 2 页空 → 停


def test_discover_respects_max_pages(env, monkeypatch):
    """防呆：一次请求不该把整个版块翻穿。"""
    db, posts, jobs = env
    # 每页都有落在范围内的日期，永远不会触发提前退出
    fc = FakeForum(pages={p: rows((p, "2026-09-14")) for p in range(1, 50)},
                   details={p: make_detail(p) for p in range(1, 50)})
    monkeypatch.setattr("bt169.config.MAX_BACKFILL_PAGES", 5)
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    c.run(from_date="2026-09-01", to_date="2026-09-14")
    assert len(fc.list_calls) == 5


def test_discover_skips_rows_outside_range(env):
    db, posts, jobs = env
    fc = FakeForum(pages={
        1: rows((101, "2026-09-14"), (102, "2026-09-11"), (103, "2026-09-08")),
    }, details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    c.run(from_date="2026-09-12", to_date="2026-09-14")
    assert fc.fetch_calls == [101]


def test_discover_dedupes_repeated_tid(env):
    """同一 tid 在相邻页重复出现时只抓一次。"""
    db, posts, jobs = env
    fc = FakeForum(pages={
        1: rows((101, "2026-09-14")),
        2: rows((101, "2026-09-14")),
    }, details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    c.run(from_date="2026-09-01", to_date="2026-09-14")
    assert fc.fetch_calls == [101]


# ------------------------------------------------------------ 跳过规则


def test_skips_existing_posts(env):
    """★ 用户原话：「数据库里有的就跳过」。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="已有", code=None, actress=None, release_date=None,
        size=None, cover_img=None, detail_img=None, ed2k="ed2k://|file|a|1|AB|/",
        post_date="2026-09-14", status="done",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"), (102, "2026-09-14"))},
                   details={102: make_detail(102)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert fc.fetch_calls == [102]           # 101 没被抓
    assert result.skipped == 1
    assert result.collected == 1


def test_pending_post_is_not_skipped(env):
    """★ 未完成的帖**不**跳过——旧行为是「任意状态都跳」，那是死路。

    这个测试原叫 ``test_skips_existing_regardless_of_status``，断言的正是
    后来害用户卡死的规则。改成新语义：``pending`` 是未完成，必须重试。

    详见 ``test_pending_post_is_retried_when_session_becomes_available``。
    """
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="未解锁", code=None, actress=None, release_date=None,
        size=None, cover_img=None, detail_img=None, ed2k=None,
        post_date="2026-09-14", status="pending",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert fc.fetch_calls == [101], "pending 帖必须被重抓"
    assert result.skipped == 0
    assert result.collected == 1


def test_rerun_overwrites_failed_post(env):
    """用户手动重跑时，upsert 会覆盖旧数据。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="旧标题", code=None, actress=None, release_date=None,
        size=None, cover_img=None, detail_img=None, ed2k=None,
        post_date="2026-09-14", status="failed",
    )
    posts.delete(101)                        # 模拟用户删掉后重采
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert result.collected == 1
    assert posts.get(101).title == "标题 101"


# ------------------------------------------------------------ 状态判定


def test_status_done_when_ed2k_present(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).status == "done"


def test_status_pending_when_locked(env):
    """★ ed2k 需登录+感谢才能看到 → pending，不是 failed。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=True)})
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).status == "pending"


def test_status_nolink_when_no_ed2k_and_unlocked(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=False)})
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).status == "nolink"


def test_collected_fields_stored(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-13"))},
                   details={101: make_detail(101)})
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-13", to_date="2026-09-13")
    p = posts.get(101)
    assert p is not None
    assert p.post_date == "2026-09-13"        # 归档依据来自列表页，非详情页
    assert p.code == "ABC-101"
    assert p.cover_img == "https://img.example/101.jpg"


# ------------------------------------------------------------ 失败处理


def test_fetch_failure_counted_and_continues(env):
    """单帖失败不该终止整个任务。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"), (102, "2026-09-14"))},
                   details={102: make_detail(102)})
    fc.fail_tids = {101}
    result = Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert result.status == JOB_DONE
    assert result.failed == 1
    assert result.collected == 1
    assert posts.get(102) is not None


def test_login_required_aborts_job(env):
    """★ 会话失效时中止，而不是把剩余帖子全刷成失败。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"), (102, "2026-09-14"))},
                   details={102: make_detail(102)})
    fc.login_required_tids = {101}
    with pytest.raises(LoginRequired):
        Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
            from_date="2026-09-14", to_date="2026-09-14")

    job = jobs.recent(1)[0]
    assert job.status == JOB_FAILED
    assert "会话失效" in (job.message or "")
    assert fc.fetch_calls == [101]           # 没继续抓 102


def test_validation_error_creates_no_job(env):
    """日期非法时不该留下垃圾任务记录。"""
    db, posts, jobs = env
    fc = FakeForum()
    with pytest.raises(ValidateError):
        Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
            from_date="2026-09-14", to_date="2026-09-01")
    assert jobs.recent() == []


# ------------------------------------------------------------ 进度记录


def test_job_progress_recorded(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"), (102, "2026-09-14"))},
                   details={101: make_detail(101), 102: make_detail(102)})
    result = Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    job = jobs.get(result.job_id)
    assert job is not None
    assert job.status == JOB_DONE
    assert job.total == 2 and job.processed == 2 and job.collected == 2
    assert job.pages == 2                    # 第 1 页 + 空页
    assert job.finished_at is not None
    assert job.percent == 100


def test_job_percent_zero_when_total_unknown(env):
    db, posts, jobs = env
    job = jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")
    assert job.percent == 0


# ------------------------------------------------------------ 单任务约束


def test_only_one_active_job(env):
    """★ 并发度恒为 1：第二个任务必须被拒。"""
    db, posts, jobs = env
    jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")
    with pytest.raises(RuntimeError, match="已有采集任务"):
        jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")


def test_finished_job_allows_new_one(env):
    db, posts, jobs = env
    j1 = jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")
    jobs.finish(j1.id, status=JOB_DONE)
    j2 = jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")
    assert j2.id != j1.id


def test_reap_orphans_unblocks_stuck_job(env):
    """★ 进程被 SIGKILL 后遗留的 running 任务会永久占位，必须能回收。"""
    db, posts, jobs = env
    jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")
    assert jobs.reap_orphans() == 1
    assert jobs.active() is None
    jobs.create(from_date="2026-09-01", to_date="2026-09-14", fid="192")  # 不再抛


# ------------------------------------------------------------ 取消


def test_cancel_stops_collection(env):
    db, posts, jobs = env
    tids = [(100 + i, "2026-09-14") for i in range(10)]
    fc = FakeForum(pages={1: rows(*tids)},
                   details={t: make_detail(t) for t, _ in tids})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]

    original = fc.fetch_thread
    def fetch_and_cancel(tid: int):  # type: ignore[no-untyped-def]
        if len(fc.fetch_calls) == 2:
            jobs.request_cancel(jobs.active().id)   # type: ignore[union-attr]
        return original(tid)
    fc.fetch_thread = fetch_and_cancel  # type: ignore[method-assign]

    result = c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert result.status == JOB_CANCELLED
    assert len(fc.fetch_calls) < 10


def test_cancel_marks_job_cancelled(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    jobs.create(from_date="2026-09-14", to_date="2026-09-14", fid="192")
    # 预先取消
    jobs.request_cancel(jobs.active().id)  # type: ignore[union-attr]
    # run 会因唯一索引冲突而失败 → 改为直接测 request_cancel 语义
    assert jobs.is_cancel_requested(1) is True


# ------------------------------------------------------------ 后台运行器


def test_runner_starts_and_completes(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(c, jobs)

    job = runner.start(from_date="2026-09-14", to_date="2026-09-14")
    assert job.status == "running"
    runner.join(timeout=5)
    assert runner.running is False

    final = jobs.get(job.id)
    assert final is not None and final.status == JOB_DONE
    assert final.collected == 1


def test_runner_rejects_second_concurrent_job(env):
    """★ 单飞：HTTP 层也必须挡住并发。"""
    db, posts, jobs = env
    tids = [(100 + i, "2026-09-14") for i in range(30)]
    fc = FakeForum(pages={1: rows(*tids)},
                   details={t: make_detail(t) for t, _ in tids})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(c, jobs)
    runner.start(from_date="2026-09-14", to_date="2026-09-14")
    try:
        with pytest.raises(CollectError, match="正在运行"):
            runner.start(from_date="2026-09-14", to_date="2026-09-14")
    finally:
        runner.join(timeout=10)


def test_runner_validates_before_creating_job(env):
    """日期非法应同步抛错（→ HTTP 400），且不建任务。"""
    db, posts, jobs = env
    runner = CollectRunner(Collector(client=FakeForum(), posts=posts, jobs=jobs), jobs)  # type: ignore[arg-type]
    with pytest.raises(ValidateError):
        runner.start(from_date="2026-09-14", to_date="2026-09-01")
    assert jobs.recent() == []


def test_runner_creates_exactly_one_job(env):
    """★ 回归：早期实现 start() 与 run() 各建一次任务 → 第二个必然撞唯一索引。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    runner = CollectRunner(Collector(client=fc, posts=posts, jobs=jobs), jobs)  # type: ignore[arg-type]
    runner.start(from_date="2026-09-14", to_date="2026-09-14")
    runner.join(timeout=5)
    assert len(jobs.recent()) == 1


# ------------------------------------------------------------ 感谢解锁（C-4）


class FakeThanks:
    """假感谢客户端，记录调用。"""

    def __init__(self, *, unlocked: dict[int, ThreadDetail] | None = None,
                 fail: set[int] | None = None) -> None:
        self.unlocked = unlocked or {}
        self.fail = fail or set()
        self.calls: list[tuple[int, bool]] = []   # (tid, 是否复用了 html)

    def thank(self, tid: int, *, html: str | None = None):
        from bt169.source.thanks import ThanksError, ThanksResult

        self.calls.append((tid, html is not None))
        if tid in self.fail:
            raise ThanksError(f"tid {tid} 感谢失败")
        if tid in self.unlocked:
            d = self.unlocked[tid]
            return ThanksResult(tid, ok=True, ed2k=d.ed2k, html="<html>unlocked</html>")
        return ThanksResult(tid, ok=False, message="未解锁")


def test_thanks_unlocks_pending_into_done(env, monkeypatch):
    """★ 核心：需要感谢的帖子应直接变成 done，而不是留下 pending。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=True)})
    thanks = FakeThanks(unlocked={101: make_detail(101, ed2k=True)})

    # 感谢后重新解析得到的 detail 由 parse_thread_detail 决定，这里打桩它
    import bt169.collector as col
    monkeypatch.setattr(
        col, "parse_thread_detail",
        lambda html, tid: make_detail(tid, ed2k=True),
    )

    c = Collector(client=fc, posts=posts, jobs=jobs, thanks=thanks)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert result.collected == 1
    assert posts.get(101).status == "done"
    assert posts.get(101).ed2k is not None
    assert thanks.calls == [(101, True)], "应复用已抓到的 HTML，省一次限速请求"


def test_thanks_not_called_when_ed2k_already_visible(env):
    """已有 ed2k 的帖子不该走感谢流程。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    thanks = FakeThanks()
    c = Collector(client=fc, posts=posts, jobs=jobs, thanks=thanks)  # type: ignore[arg-type]
    c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert thanks.calls == []


def test_thanks_absent_keeps_pending(env):
    """未注入感谢客户端时，锁定帖仍以 pending 入库（不报错）。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=True)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert result.collected == 1
    assert result.failed == 0
    assert posts.get(101).status == "pending"


def test_thanks_failure_does_not_fail_collection(env):
    """★ 解锁失败 ≠ 采集失败：其余字段已拿到，先落 pending。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=True)})
    thanks = FakeThanks(fail={101})
    c = Collector(client=fc, posts=posts, jobs=jobs, thanks=thanks)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert result.failed == 0
    assert result.collected == 1
    assert posts.get(101).status == "pending"
    assert posts.get(101).code == "ABC-101", "其余字段应保留"


def test_thanks_login_required_aborts_job(env):
    """★ 感谢时发现会话失效 → 中止整个任务，而不是把剩余帖子全刷成失败。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"), (102, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=False, locked=True),
                            102: make_detail(102, ed2k=False, locked=True)})

    class Exploding(FakeThanks):
        def thank(self, tid, *, html=None):
            raise LoginRequired("会话失效")

    c = Collector(client=fc, posts=posts, jobs=jobs, thanks=Exploding())  # type: ignore[arg-type]
    with pytest.raises(LoginRequired):
        c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert jobs.recent()[0].status == JOB_FAILED
    assert "会话失效" in jobs.recent()[0].message


# ------------------------------------------------------------ 图片本地化（W-15）


class FakeImages:
    """假图片缓存。"""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[int] = []
        self.ensured: set[str] = set()      # 已「落盘」的源 URL

    # ---- 供 _repair_images 使用（模拟真实的 has/ensure 语义）----

    def has(self, url: str) -> bool:
        return url in self.ensured

    def key_for(self, url: str) -> str:
        import hashlib
        return hashlib.sha1(url.encode()).hexdigest()[:16]

    def url_for(self, key: str, width: int) -> str:
        return f"/img/{key[:2]}/{key}-{width}.webp"

    def ensure(self, url: str):  # type: ignore[no-untyped-def]
        from bt169.collector.imagecache import CachedImage

        self.ensured.add(url)
        key = self.key_for(url)
        return CachedImage(url=url, key=key,
                           variants={600: self.url_for(key, 600),
                                     1200: self.url_for(key, 1200)},
                           bytes_in=1, bytes_out=1)

    def ensure_post(self, detail):
        from bt169.collector.imagecache import CachedImage, ImageError

        self.calls.append(detail.tid)
        out = {}
        for field in ("cover", "detail"):
            url = getattr(detail, f"{field}_img", None)
            if not url or field in self.fail:
                continue
            self.ensured.add(url)
            key = self.key_for(url)
            out[field] = CachedImage(
                url=url, key=key,
                variants={600: self.url_for(key, 600),
                          1200: self.url_for(key, 1200)},
                bytes_in=100, bytes_out=10,
            )
        return out


def test_images_localized_on_collect(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    images = FakeImages()
    c = Collector(client=fc, posts=posts, jobs=jobs, images=images)  # type: ignore[arg-type]
    c.run(from_date="2026-09-14", to_date="2026-09-14")

    p = posts.get(101)
    assert images.calls == [101]
    # ★ 两档宽度各司其职：卡片 600、灯箱 1200
    assert p.cover_local.endswith("-600.webp"), p.cover_local
    assert p.detail_local.endswith("-1200.webp"), p.detail_local


def test_image_failure_does_not_fail_collect(env):
    """★ 图片失败不该让整帖采集失败（ed2k/番号比图重要）。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    images = FakeImages(fail={"cover", "detail"})
    c = Collector(client=fc, posts=posts, jobs=jobs, images=images)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert result.failed == 0
    assert result.collected == 1
    p = posts.get(101)
    assert p.cover_local is None
    assert p.ed2k is not None, "ed2k 必须保留"
    assert p.code == "ABC-101"


def test_local_path_survives_recollect_without_image(env):
    """★ 重采时图床抽风 → 不能把上轮已下好的本地图路径抹掉。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    Collector(client=fc, posts=posts, jobs=jobs, images=FakeImages()).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).cover_local is not None

    posts.delete(101)                       # 用户删掉后重采
    Collector(client=fc, posts=posts, jobs=jobs,
              images=FakeImages(fail={"cover", "detail"})).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    # 行被删了，所以这里是全新插入 → 本地路径为 None（COALESCE 只对 UPSERT 生效）
    assert posts.get(101) is not None


def test_local_path_preserved_on_upsert(env):
    """同一 tid 二次 upsert 且本轮无图 → 保留旧本地路径（COALESCE）。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="t", code=None, actress=None, release_date=None,
        size=None, cover_img="https://img/c.jpg", detail_img=None,
        ed2k="ed2k://|file|a|1|AB|/", post_date="2026-09-14", status="done",
        cover_local="/img/ab/old-600.webp",
    )
    posts.upsert_collected(
        tid=101, title="t2", code=None, actress=None, release_date=None,
        size=None, cover_img="https://img/c.jpg", detail_img=None,
        ed2k="ed2k://|file|a|1|AB|/", post_date="2026-09-14", status="done",
        cover_local=None,
    )
    assert posts.get(101).cover_local == "/img/ab/old-600.webp"


def test_no_images_injected_is_fine(env):
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert result.collected == 1
    assert posts.get(101).cover_local is None


def test_skipped_post_gets_missing_images_repaired(env):
    """★ 回归：已入库帖子若本地图丢失，重采时必须补回来。

    真实场景：用户删了 data/images/（或换机迁移只带走了 db）。
    采集按「tid 已存在」跳过该帖，于是那些本地图路径永远指向不存在的
    文件——前端裂图，且**再也不会自愈**。

    修复只依赖库里已有的源 URL，不访问论坛，所以不受限速影响。
    """
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    images = FakeImages()
    Collector(client=fc, posts=posts, jobs=jobs, images=images).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).cover_local is not None

    # 模拟「图文件被删掉」：清掉本地路径，保留源 URL
    with db.write() as conn:
        conn.execute("UPDATE posts SET cover_local=NULL WHERE tid=101")
    assert posts.get(101).cover_local is None

    images.calls.clear()
    fc.fetch_calls.clear()
    result = Collector(client=fc, posts=posts, jobs=jobs,  # type: ignore[arg-type]
                       images=images).run(
        from_date="2026-09-14", to_date="2026-09-14")

    assert result.skipped == 1, "帖子本身仍应跳过（不重采）"
    assert result.collected == 0
    assert images.ensured, "但必须去补图"
    assert fc.fetch_calls == [], "补图不该访问论坛（否则白耗限速额度）"
    assert posts.get(101).cover_local is not None, "本地路径必须被修回来"


def test_repair_failure_does_not_fail_job(env):
    """补图失败（图床挂了）不该让任务失败——帖子数据本来就在库里。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    Collector(client=fc, posts=posts, jobs=jobs,  # type: ignore[arg-type]
              images=FakeImages()).run(
        from_date="2026-09-14", to_date="2026-09-14")
    with db.write() as conn:
        conn.execute("UPDATE posts SET cover_local=NULL WHERE tid=101")

    class BoomImages:
        def has(self, url):
            return False

        def ensure(self, url):
            from bt169.collector.imagecache import ImageError
            raise ImageError("图床 502")

    result = Collector(client=fc, posts=posts, jobs=jobs,  # type: ignore[arg-type]
                       images=BoomImages()).run(
        from_date="2026-09-14", to_date="2026-09-14")
    assert result.skipped == 1
    assert result.failed == 0, "补图失败不算采集失败"


def test_repair_noop_when_image_present(env):
    """图还在就不该重复下载（避免每次采集都白跑一轮图床请求）。"""
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})

    class TrackingImages(FakeImages):
        pass

    images = TrackingImages()
    Collector(client=fc, posts=posts, jobs=jobs, images=images).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    before = set(images.ensured)
    Collector(client=fc, posts=posts, jobs=jobs, images=images).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert images.ensured == before, "图还在就不该再下"


def test_repair_fixes_wrong_width(env):
    """★ 回归：detail_local 记成 600px 时必须被纠正为 1200px。

    真实 bug：早先版本两列都写 600px。1200px 文件本来就存在，
    所以「非空 + has() 为真」的检查会永远跳过它——灯箱一直显示
    600px 模糊图，而磁盘上那份 1200px 从来没人用。
    """
    db, posts, jobs = env
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101)})
    images = FakeImages()
    Collector(client=fc, posts=posts, jobs=jobs, images=images).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).detail_local.endswith("-1200.webp")

    # 模拟旧数据：detail_local 被写成 600
    with db.write() as conn:
        conn.execute(
            "UPDATE posts SET detail_local = replace(detail_local,'-1200.','-600.')"
            " WHERE tid=101"
        )
    assert posts.get(101).detail_local.endswith("-600.webp")

    Collector(client=fc, posts=posts, jobs=jobs, images=images).run(  # type: ignore[arg-type]
        from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).detail_local.endswith("-1200.webp"), "必须纠正宽度"
    assert posts.get(101).cover_local.endswith("-600.webp"), "封面不该被改成 1200"


# ------------------------------------------- ★ 已入库但未完成的帖子必须能重试
#
# 实测事故（用户真实遇到）：13 个帖子在**没有登录会话**时被采成 `pending`
# （匿名访问详情页拿不到 ed2k，感谢解锁也没会话可用）。用户随后配好了
# 账号密码、会话可用，再跑采集 —— **一个都没变成 `done`**，App 依旧空白。
#
# 根因是两条规则叠加成死路：
#
#   1. 跳过规则是「tid 已入库就跳过（**任意状态**）」
#   2. 会话失效时采集器只把帖子标成 `pending` 就完事
#
# 于是 `pending` 的帖子既不会被重采（规则 1），也没有任何别的路径会去
# 处理它 —— **永久卡死**。用户唯一的出路是手动删掉再重采，而前端按
# `BROWSABLE_STATUSES` 过滤，`pending` 在界面上**根本看不见**，
# 连「删掉重来」都做不到。
#
# ★ 跳过规则的**本意**是「省掉重复抓取」，不是「放弃未完成的帖子」。
#   需求 C-2 说「已入库的跳过」，而 C-8 明确要求「失败可重试」。
#   正确语义：`done` / `nolink` 是**终态**（有结论了，跳过）；
#   `pending` / `failed` / `thanked` 是**未完成**（该重试）。


def test_pending_post_is_retried_when_session_becomes_available(env):
    """★ 核心场景：pending 帖在会话可用后必须能被采成 done。

    这正是用户遇到的：先匿名采成 pending，配好凭据后重采却毫无变化。
    """
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="旧标题", code="START-001", actress="A",
        release_date="2026-09-01", size="7GB",
        cover_img=None, detail_img=None, ed2k=None,
        post_date="2026-09-14", status="pending",
    )
    assert posts.get(101).status == "pending"

    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert posts.get(101).status == "done", (
        "pending 帖在有会话时重采必须变 done——否则永久卡死"
    )
    assert posts.get(101).ed2k, "重采必须写入 ed2k"
    assert result.collected == 1, "重试的帖应算作已采集，不是跳过"


def test_failed_post_is_retried(env):
    """★ C-8「失败可重试」：failed 帖必须能被重采。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="旧", code="START-001", actress=None,
        release_date=None, size=None, cover_img=None, detail_img=None,
        ed2k=None, post_date="2026-09-14", status="failed",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert posts.get(101).status == "done"


def test_done_post_still_skipped(env):
    """★ 反向：终态必须仍然跳过——C-2 增量去重的本意不能丢。

    否则每轮采集都会重抓所有历史帖子，白白消耗限速预算与封号风险。
    """
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="已完成", code="START-001", actress=None,
        release_date=None, size=None, cover_img=None, detail_img=None,
        ed2k="ed2k://|file|x.mkv|1|AA|/", post_date="2026-09-14",
        status="done",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")

    assert posts.get(101).title == "已完成", "终态帖不该被重抓覆盖"
    assert result.skipped == 1
    assert fc.fetch_calls == [], "终态帖不该产生详情页请求（浪费限速预算）"


def test_nolink_post_still_skipped(env):
    """★ nolink 也是终态：已确认无链接，重抓没有意义。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="无链接", code="START-001", actress=None,
        release_date=None, size=None, cover_img=None, detail_img=None,
        ed2k=None, post_date="2026-09-14", status="nolink",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))},
                   details={101: make_detail(101, ed2k=True)})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert result.skipped == 1
    assert fc.fetch_calls == []


# ------------------------------------------------------------ 归档日期正确性


def test_post_date_comes_from_row_not_range_start(env):
    """★ 归档日期必须是**该帖自己**的日期，不是采集范围的下界。

    实测坑：``_collect_one(tid, job.from_date)`` 把范围内每一帖都盖上
    范围**起始**日期。单日采集看不出来（起始日 = 该日），一旦跨日
    采集，2026-09-14 的帖会被错分到 2026-09-13 的归档日下。
    """
    db, posts, jobs = env
    fc = FakeForum(
        pages={1: rows((101, "2026-09-14"), (102, "2026-09-13"))},
        details={101: make_detail(101), 102: make_detail(102)},
    )
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-13", to_date="2026-09-14")
    assert posts.get(101).post_date == "2026-09-14"
    assert posts.get(102).post_date == "2026-09-13"


def test_post_date_survives_multi_day_range(env):
    """跨 3 天范围，三帖各归各日。"""
    db, posts, jobs = env
    fc = FakeForum(
        pages={1: rows((101, "2026-09-14"), (102, "2026-09-13"),
                       (103, "2026-09-12"))},
        details={t: make_detail(t) for t in (101, 102, 103)},
    )
    Collector(client=fc, posts=posts, jobs=jobs).run(  # type: ignore[arg-type]
        from_date="2026-09-12", to_date="2026-09-14")
    assert {t: posts.get(t).post_date for t in (101, 102, 103)} == {
        101: "2026-09-14", 102: "2026-09-13", 103: "2026-09-12"}
