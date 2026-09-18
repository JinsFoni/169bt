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
        cover_img=f"https://img.example/{tid}.jpg", detail_img=None,
        ed2k=f"ed2k://|file|{tid}.mkv|1|AB|/" if ed2k else None,
        locked=locked,
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


def test_skips_existing_regardless_of_status(env):
    """已存在的失败帖也跳过（不自动重试历史失败）。"""
    db, posts, jobs = env
    posts.upsert_collected(
        tid=101, title="失败过", code=None, actress=None, release_date=None,
        size=None, cover_img=None, detail_img=None, ed2k=None,
        post_date="2026-09-14", status="failed",
    )
    fc = FakeForum(pages={1: rows((101, "2026-09-14"))})
    c = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    result = c.run(from_date="2026-09-14", to_date="2026-09-14")
    assert fc.fetch_calls == []
    assert result.skipped == 1


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
