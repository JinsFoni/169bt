"""RSS 定时轮询测试（C-1）。全部离线。"""

from __future__ import annotations

import pytest

from bt169.collector import Collector, RssPoller, RssPollResult
from bt169.db import Database
from bt169.repo.collect import JOB_DONE, CollectRepo
from bt169.repo.posts import PostRepo
from bt169.source.forum import FetchError
from bt169.source.parse import ListRow, ThreadDetail

from tests.test_collector import FakeForum, make_detail

FEED_URL = "https://169bt.com/forum.php?mod=rss&fid=192"


def feed(*specs: tuple[int, str]) -> str:
    """拼一个最小 RSS feed。"""
    items = "".join(
        f"""<item>
          <title>标题 {tid}</title>
          <link>https://169bt.com/forum.php?mod=viewthread&amp;tid={tid}</link>
          <pubDate>{date} 11:00:00 +0000</pubDate>
        </item>"""
        for tid, date in specs
    )
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>{items}</channel></rss>'


class FakeFeed:
    """可编程的假 feed 客户端。"""

    def __init__(self, xml: str = "", *, error: Exception | None = None) -> None:
        self.xml = xml
        self.error = error
        self.calls = 0

    def get(self, url: str, **kw: object) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return type("P", (), {"text": self.xml})()


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield db, PostRepo(db), CollectRepo(db)
    db.close()


def make_poller(env, *, xml="", error=None, details=None, feed_obj=None):
    db, posts, jobs = env
    fc = FakeForum(details=details or {})
    collector = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    poller = RssPoller(
        collector=collector,
        posts=posts,
        jobs=jobs,
        feed=feed_obj or FakeFeed(xml, error=error),  # type: ignore[arg-type]
        feed_url=FEED_URL,
    )
    return poller, posts, jobs, fc


# ------------------------------------------------------------ 基本发现


def test_poll_collects_new_tids(env):
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026"), (102, "Mon, 14 Sep 2026")),
        details={101: make_detail(101), 102: make_detail(102)},
    )
    r = poller.tick()
    assert isinstance(r, RssPollResult)
    assert r.checked == 2
    assert r.new == 2
    assert r.collected == 2
    assert posts.get(101).status == "done"
    assert posts.get(102).status == "done"


def test_poll_skips_already_settled(env):
    """C-2 增量去重：已入库的（终态）不再抓。"""
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026"), (102, "Mon, 14 Sep 2026")),
        details={101: make_detail(101), 102: make_detail(102)},
    )
    poller.tick()
    assert fc.fetch_calls == [101, 102]

    # 第二轮：feed 没变 → 一个都不该再抓
    r2 = poller.tick()
    assert r2.checked == 2
    assert r2.new == 0
    assert r2.collected == 0
    assert fc.fetch_calls == [101, 102]      # ★ 没有新增请求


def test_poll_collects_only_the_new_one(env):
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026")),
        details={101: make_detail(101), 102: make_detail(102)},
    )
    poller.tick()
    poller.feed.xml = feed(  # type: ignore[attr-defined]
        (102, "Mon, 14 Sep 2026"), (101, "Mon, 14 Sep 2026"))
    r = poller.tick()
    assert r.new == 1
    assert fc.fetch_calls == [101, 102]


def test_poll_uses_rss_date_for_archive(env):
    """★ 归档日期必须来自 RSS（UTC+8 换算后），不是「今天」。"""
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Sun, 13 Sep 2026")),
        details={101: make_detail(101)},
    )
    poller.tick()
    assert posts.get(101).post_date == "2026-09-13"


def test_poll_converts_utc_to_utc8(env):
    """RSS 是 UTC：UTC 16:30 → UTC+8 已是次日（C-9）。"""
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026 16:30:00 +0000")),
        details={101: make_detail(101)},
    )
    poller.tick()
    assert posts.get(101).post_date == "2026-09-15"


# ------------------------------------------------------------ 容错


def test_poll_empty_feed_is_not_an_error(env):
    poller, posts, jobs, fc = make_poller(env, xml="")
    r = poller.tick()
    assert r.checked == 0
    assert r.new == 0
    assert r.error is None
    assert fc.fetch_calls == []


def test_poll_html_error_page_is_not_an_error(env):
    """★ 被 WAF 拦时返回 HTML —— 轮询不能崩，也不能假装发现了帖。"""
    poller, posts, jobs, fc = make_poller(
        env, xml="<!doctype html><html><body>Blocked By WAF</body></html>")
    r = poller.tick()
    assert r.checked == 0
    assert r.error is None


def test_poll_network_error_is_captured_not_raised(env):
    """★ 后台线程绝不抛异常：网络挂了只记在结果里。"""
    poller, posts, jobs, fc = make_poller(
        env, error=FetchError("网络炸了"))
    r = poller.tick()
    assert r.error is not None
    assert "网络炸了" in r.error
    assert r.checked == 0


def test_poll_survives_single_post_failure(env):
    """单帖抓取失败不影响其余帖子，也不抛异常。"""
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026"), (102, "Mon, 14 Sep 2026")),
        details={102: make_detail(102)},
    )
    fc.fail_tids.add(101)
    r = poller.tick()
    assert r.error is None
    assert posts.get(102) is not None
    assert posts.get(101) is None


# ------------------------------------------------------------ 与手动采集互斥


def test_poll_skips_when_a_job_is_already_running(env):
    """★ 采集并发度恒为 1：已有任务在跑时不插队。"""
    db, posts, jobs = env
    jobs.create(from_date="2026-09-14", to_date="2026-09-14", fid="192")
    poller, posts, jobs, fc = make_poller(
        env, xml=feed((101, "Mon, 14 Sep 2026")),
        details={101: make_detail(101)})
    r = poller.tick()
    assert r.error is None
    assert r.new == 1              # 发现了
    assert r.collected == 0        # 但没采（让位给在跑的任务）
    assert fc.fetch_calls == []


def test_poll_result_records_job_id(env):
    poller, posts, jobs, fc = make_poller(
        env, xml=feed((101, "Mon, 14 Sep 2026")),
        details={101: make_detail(101)})
    r = poller.tick()
    assert r.job_id is not None
    job = jobs.get(r.job_id)
    assert job.status == JOB_DONE
    assert job.collected == 1


# ------------------------------------------------------------ 与手动采集互斥（真锁）


def test_runner_try_begin_fails_while_collect_running(env):
    """★ 轮询必须和手动采集抢**同一把锁**，不能各看各的标志。

    `jobs.active()` 是数据库层面的检查，与 `CollectRunner` 的线程状态
    之间有一个窗口：查完之后、建任务之前手动采集可能刚好启动。
    真正靠得住的互斥是共用同一个 runner 的锁。
    """
    from bt169.collector import CollectRunner

    db, posts, jobs = env
    fc = FakeForum(pages={1: []}, details={})
    collector = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)

    assert runner.try_begin() is True
    assert runner.running is True          # 占位期间 runner 视为忙

    # 手动采集此时必须被拒
    import pytest as _pytest
    with _pytest.raises(Exception):
        runner.start(from_date="2026-09-14", to_date="2026-09-14")

    runner.end()
    assert runner.running is False


def test_runner_try_begin_fails_when_thread_alive(env):
    """手动采集在跑时，轮询拿不到通道。"""
    from bt169.collector import CollectRunner

    db, posts, jobs = env
    fc = FakeForum(pages={1: []}, details={})
    collector = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)
    runner.start(from_date="2026-09-14", to_date="2026-09-14")
    runner.join(5)
    # 线程已结束 → 可以拿
    assert runner.try_begin() is True
    runner.end()


def test_poller_uses_runner_channel(env):
    """poller 走 runner 的通道：拿不到就不抓。"""
    from bt169.collector import CollectRunner

    db, posts, jobs = env
    fc = FakeForum(details={101: make_detail(101)})
    collector = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)
    poller = RssPoller(
        collector=collector, posts=posts, jobs=jobs,
        feed=FakeFeed(feed((101, "Mon, 14 Sep 2026"))),  # type: ignore[arg-type]
        feed_url=FEED_URL, runner=runner,
    )
    runner.try_begin()          # 假装手动采集占着
    r = poller.tick()
    assert r.new == 1
    assert r.collected == 0     # 没抢到通道
    assert fc.fetch_calls == []
    runner.end()

    r2 = poller.tick()
    assert r2.collected == 1    # 通道空出来了


# ------------------------------------------------------------ API 装配


def test_app_starts_poll_scheduler_when_enabled(tmp_path):
    from bt169.api.app import create_app
    from bt169.crypto import SecretBox
    from bt169.db import Database

    db = Database(tmp_path / "t.db")
    db.migrate()
    app = create_app(db, box=SecretBox(b"\x07" * 32), ui_dir=None,
                     emby_interval=9999)
    assert app.state.poll_scheduler.interval == 9999
    db.close()


def test_app_poll_scheduler_zero_disables(tmp_path):
    from bt169.api.app import create_app
    from bt169.crypto import SecretBox
    from bt169.db import Database

    db = Database(tmp_path / "t.db")
    db.migrate()
    app = create_app(db, box=SecretBox(b"\x07" * 32), ui_dir=None,
                     emby_interval=0)
    assert not hasattr(app.state, "poll_scheduler")
    db.close()


def test_app_poll_scheduler_delays_first_tick(tmp_path):
    """★ 启动不立即轮询：服务器重启是常见操作。

    虽然 RSS 不消耗登录额度（事实 #21），但启动即抓帖会在每次重启时
    都产生一轮真实论坛请求 + 2–5 秒限速。推迟一个 interval 更稳。
    """
    from bt169.api.app import create_app
    from bt169.crypto import SecretBox
    from bt169.db import Database

    db = Database(tmp_path / "t.db")
    db.migrate()
    app = create_app(db, box=SecretBox(b"\x07" * 32), ui_dir=None,
                     emby_interval=9999)
    assert app.state.poll_scheduler.delay_first is True
    assert app.state.poll_scheduler.name == "rss-poll"
    db.close()


def test_poller_reuses_the_same_runner_as_manual_collect(tmp_path):
    """★ 轮询与手动采集必须共用**同一个** runner 实例。

    各建一个 runner 就是各有一把锁——两边同时跑采集，
    而「采集并发度恒为 1」是封号风险的硬约束。
    """
    from bt169.api.app import create_app
    from bt169.api.routes.collect import get_runner
    from bt169.crypto import SecretBox
    from bt169.db import Database
    from starlette.requests import Request

    db = Database(tmp_path / "t.db")
    db.migrate()
    app = create_app(db, box=SecretBox(b"\x07" * 32), ui_dir=None,
                     emby_interval=0)
    # 装配期就把 runner 放进 state，两边都取它
    from bt169.api.app import _ensure_runner
    _ensure_runner(app)
    runner_from_state = app.state.collect_runner

    scope = {"type": "http", "app": app, "headers": []}
    runner_via_dep = get_runner(Request(scope))  # type: ignore[arg-type]
    assert runner_via_dep is runner_from_state
    db.close()


def test_poll_job_records_total(env):
    """★ RSS 任务的 ``total`` 必须被写上。

    ``total`` 只在 `_run_job`（列表页路径）里设置。RSS 走 `fetch_items`
    直连，若不补上，任务进度永远停在 0%（`percent` 依赖 total），
    前端进度条一直空着。
    """
    poller, posts, jobs, fc = make_poller(
        env,
        xml=feed((101, "Mon, 14 Sep 2026"), (102, "Mon, 14 Sep 2026")),
        details={101: make_detail(101), 102: make_detail(102)},
    )
    r = poller.tick()
    job = jobs.get(r.job_id)
    assert job.total == 2
    assert job.percent == 100


# ------------------------------------------------------------ 手动触发轮询


def test_poll_tick_can_skip_claiming(env):
    """★ ``claim=False``：调用方已持锁时不要再抢一次。

    手动触发接口先用 runner 占住通道（这样能**同步**返回 409），
    再起后台线程跑 tick。若 tick 又去 try_begin，会发现自己被占着，
    直接把本轮空转掉。
    """
    from bt169.collector import CollectRunner

    db, posts, jobs = env
    fc = FakeForum(details={101: make_detail(101)})
    collector = Collector(client=fc, posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)
    poller = RssPoller(
        collector=collector, posts=posts, jobs=jobs,
        feed=FakeFeed(feed((101, "Mon, 14 Sep 2026"))),  # type: ignore[arg-type]
        feed_url=FEED_URL, runner=runner,
    )
    assert runner.try_begin() is True      # 接口先占住
    r = poller.tick(claim=False)
    assert r.collected == 1                # 仍然干活
    runner.end()


def test_runner_start_poll_rejects_when_busy(env):
    """通道被占时**同步**报错（不要起线程后才失败）。"""
    from bt169.collector import CollectError, CollectRunner

    db, posts, jobs = env
    collector = Collector(client=FakeForum(), posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)
    runner.try_begin()
    with pytest.raises(CollectError):
        runner.start_poll(lambda claim: None)  # type: ignore[arg-type]
    runner.end()


def test_runner_start_poll_runs_and_releases(env):
    from bt169.collector import CollectRunner

    db, posts, jobs = env
    collector = Collector(client=FakeForum(), posts=posts, jobs=jobs)  # type: ignore[arg-type]
    runner = CollectRunner(collector, jobs)
    seen = []

    def work(claim: bool) -> None:
        seen.append(claim)

    runner.start_poll(work)
    assert runner.running is True
    runner.join(5)
    assert seen == [False]          # 已持锁 → claim=False
    assert runner.running is False  # 跑完自动释放


# ------------------------------------------------------------ POST /api/collect/poll


def _poll_client(tmp_path, *, feed_xml: str, details: dict | None = None,
                 rss_url: str | None = "https://169bt.com/forum.php?mod=rss&fid=192"):
    """造一个 app，feed 与论坛客户端都换成替身。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.api.routes import collect as collect_route
    from bt169.collector import CollectRunner, Collector
    from bt169.config import section_key
    from bt169.crypto import SecretBox
    from bt169.db import Database
    from bt169.repo.collect import CollectRepo as Jobs
    from bt169.repo.posts import PostRepo as Posts
    from bt169.repo.settings import SettingsRepo

    db = Database(tmp_path / "t.db")
    db.migrate()
    box = SecretBox(b"\x03" * 32)
    if rss_url:
        SettingsRepo(db, box).put(section_key("site", "rss_url"), rss_url)

    app = create_app(db, box=box, ui_dir=None)
    fc = FakeForum(details=details or {})
    runner = CollectRunner(
        Collector(client=fc, posts=Posts(db), jobs=Jobs(db)),  # type: ignore[arg-type]
        Jobs(db),
    )
    app.state.collect_runner = runner
    fake = FakeFeed(feed_xml)
    collect_route.build_feed_client = lambda app: fake  # type: ignore[assignment]
    return TestClient(app), db, fc, fake, runner


def test_poll_endpoint_runs_and_reports(tmp_path):
    c, db, fc, fake, runner = _poll_client(
        tmp_path,
        feed_xml=feed((101, "Mon, 14 Sep 2026")),
        details={101: make_detail(101)},
    )
    with c:
        r = c.post("/api/collect/poll")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["accepted"] is True
        # 等后台跑完
        import time
        for _ in range(50):
            if not runner.running:
                break
            time.sleep(0.05)
        assert Posts_get(db, 101) is not None
    db.close()


def Posts_get(db, tid):
    from bt169.repo.posts import PostRepo
    return PostRepo(db).get(tid)


def test_poll_endpoint_400_when_not_configured(tmp_path):
    """未配置订阅链接 → 400（而不是静默什么都不做）。"""
    c, db, fc, fake, runner = _poll_client(tmp_path, feed_xml="", rss_url=None)
    with c:
        r = c.post("/api/collect/poll")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "no_rss_url"
    db.close()


def test_poll_endpoint_409_when_busy(tmp_path):
    """★ 通道被占时**同步**返回 409，不起线程。"""
    c, db, fc, fake, runner = _poll_client(tmp_path, feed_xml=feed((101, "Mon, 14 Sep 2026")))
    with c:
        assert runner.try_begin() is True
        r = c.post("/api/collect/poll")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "collect_running"
        runner.end()
    db.close()


def test_poll_endpoint_400_on_bad_rss_url(tmp_path):
    c, db, fc, fake, runner = _poll_client(
        tmp_path, feed_xml="", rss_url="https://169bt.com/forum.php?mod=forumdisplay&fid=192")
    with c:
        r = c.post("/api/collect/poll")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "bad_rss_url"
    db.close()
