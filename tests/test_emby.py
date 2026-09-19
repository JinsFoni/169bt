"""Emby 入库标记（P7 / E-1~E-7）。

全部离线：注入假 HTTP transport，不打真实 Emby。
"""

from __future__ import annotations

import json

import pytest

from bt169.emby import (
    EmbyClient,
    EmbyError,
    EmbyNotConfigured,
    EmbyScheduler,
    EmbySyncer,
    SyncResult,
    index_items,
)


class FakeHTTP:
    def __init__(self, *, status=200, body=None, raise_exc=None):
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, params or {}, headers or {}))
        if self.raise_exc is not None:
            raise self.raise_exc

        class R:
            pass

        r = R()
        r.status_code = self.status
        r.text = self.body if self.body is not None else "{}"
        return r


def items(*specs):
    """specs: (Id, Name) 或 (Id, Name, Path)"""
    out = []
    for s in specs:
        d = {"Id": s[0], "Name": s[1]}
        if len(s) > 2:
            d["Path"] = s[2]
        out.append(d)
    return json.dumps({"Items": out, "TotalRecordCount": len(out)})


@pytest.fixture()
def db(tmp_path):
    from bt169.db import Database

    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


def seed(db, tid, code, date="2026-09-14"):
    from bt169.repo.posts import now_iso

    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,code,actress,release_date,size,"
            " cover_img,detail_img,ed2k,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"标题 {tid}", code, "某", "2026-09-17", "7GB",
             None, None, "ed2k://|file|a|1|A|/", date, "done",
             now_iso(), now_iso()),
        )


# ---------------------------------------------------------------- 索引构造


def test_index_items_maps_code_to_id():
    rows = [
        {"Id": "e1", "Name": "START-624 配送中NTR"},
        {"Id": "e2", "Name": "[4K] MIDA-737 [中文字幕]"},
    ]
    idx = index_items(rows)
    assert idx["START-624"] == "e1"
    assert idx["MIDA-737"] == "e2"


def test_index_items_uses_path_too():
    """★ 条目名可能不含番号，番号在文件路径里。

    实测：Emby 的 ``Name`` 常是清洗过的标题，``Path`` 才是原始文件名
    ``s169bbs.com@START-624_[4K].mkv``。只看 Name 会漏掉大量条目。
    """
    rows = [{"Id": "e1", "Name": "无番号标题",
             "Path": "/media/s169bbs.com@START-624_[4K].mkv"}]
    assert index_items(rows)["START-624"] == "e1"


def test_index_items_does_not_confuse_prefix():
    """E-5：START-62 不得命中 START-624。"""
    idx = index_items([{"Id": "e1", "Name": "START-624"}])
    assert "START-62" not in idx
    assert idx["START-624"] == "e1"


def test_index_items_empty_name_is_safe():
    assert index_items([{"Id": "e1", "Name": None}]) == {}
    assert index_items([{"Id": "e1"}]) == {}


def test_index_items_missing_id_is_skipped():
    """★ 没有 Id 的条目无法用于匹配，跳过而不是塞 None 进索引。"""
    idx = index_items([{"Name": "START-624"}])
    assert idx == {}


# ---------------------------------------------------------------- 客户端


def test_client_requires_url_and_key():
    for url, key in (("", "k"), ("u", ""), ("", "")):
        with pytest.raises(EmbyNotConfigured):
            EmbyClient(url=url, api_key=key, http=FakeHTTP())


def test_client_sends_api_key_header():
    http = FakeHTTP(body=items(("e1", "START-624")))
    c = EmbyClient(url="http://emby.local:8096", api_key="SECRET", http=http)
    c.list_items()

    url, params, headers = http.calls[0]
    assert url == "http://emby.local:8096/Items"
    # ★ Emby 用 X-Emby-Token 头认证，不是 query 参数——
    #   query 会进服务器访问日志，把密钥写进明文日志
    assert headers.get("X-Emby-Token") == "SECRET"
    assert "SECRET" not in url


def test_client_sends_required_params():
    http = FakeHTTP(body=items(("e1", "START-624")))
    c = EmbyClient(url="http://e", api_key="k", http=http)
    c.list_items()
    _, params, _ = http.calls[0]
    assert params["IncludeItemTypes"] == "Movie"
    assert params["Recursive"] == "true"
    assert "Fields" in params and "Path" in params["Fields"]


def test_client_uses_library_parent_when_given():
    """E-4：只匹配设置页指定的媒体库。"""
    http = FakeHTTP(body=items(("e1", "START-624")))
    c = EmbyClient(url="http://e", api_key="k", http=http,
                   library_id="lib42")
    c.list_items()
    _, params, _ = http.calls[0]
    assert params["ParentId"] == "lib42"


def test_client_no_parent_without_library():
    http = FakeHTTP(body=items(("e1", "START-624")))
    c = EmbyClient(url="http://e", api_key="k", http=http)
    c.list_items()
    _, params, _ = http.calls[0]
    assert "ParentId" not in params


def test_client_http_error_raises():
    c = EmbyClient(url="http://e", api_key="k", http=FakeHTTP(status=401))
    with pytest.raises(EmbyError) as ei:
        c.list_items()
    assert "401" in str(ei.value)


def test_client_network_error_wrapped():
    """★ E-7：Emby 不可达不该抛 httpx 异常穿透到浏览路径。"""
    c = EmbyClient(url="http://e", api_key="k",
                   http=FakeHTTP(raise_exc=OSError("连接被拒")))
    with pytest.raises(EmbyError) as ei:
        c.list_items()
    assert "连接被拒" in str(ei.value)


def test_client_non_json_raises():
    c = EmbyClient(url="http://e", api_key="k",
                   http=FakeHTTP(status=200, body="<html>oops</html>"))
    with pytest.raises(EmbyError):
        c.list_items()


def test_client_url_trailing_slash_stripped():
    http = FakeHTTP(body=items(("e1", "START-624")))
    c = EmbyClient(url="http://e/", api_key="k", http=http)
    c.list_items()
    assert "//Items" not in http.calls[0][0]


def test_client_lists_all_pages():
    """★ 媒体库超过一页时必须翻页，否则只标记第一页的帖。

    Emby 默认 Limit=... 一页几十条，一个大媒体库轻松几千条。
    """
    page1 = json.dumps({"Items": [{"Id": "e1", "Name": "START-624"}],
                        "TotalRecordCount": 2})
    page2 = json.dumps({"Items": [{"Id": "e2", "Name": "MIDA-737"}],
                        "TotalRecordCount": 2})
    http = FakeHTTP(body=page1)

    calls = []

    def get(url, *, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        class R: pass
        r = R(); r.status_code = 200
        r.text = page1 if len(calls) == 1 else page2
        return r

    http.get = get  # type: ignore[method-assign]
    c = EmbyClient(url="http://e", api_key="k", http=http)
    rows = c.list_items()

    names = {r["Name"] for r in rows}
    assert names == {"START-624", "MIDA-737"}, "必须翻页取全"
    assert len(calls) == 2


# ---------------------------------------------------------------- 同步器


def syncer(db, http):
    from bt169.repo.posts import PostRepo

    client = EmbyClient(url="http://e", api_key="k", http=http)
    return EmbySyncer(client=client, posts_repo=PostRepo(db))


def test_sync_marks_in_library(db):
    seed(db, 101, "START-624")
    seed(db, 102, "MIDA-737")

    r = syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()

    assert r.ok and r.checked == 2 and r.in_library == 1
    from bt169.repo.posts import PostRepo
    pr = PostRepo(db)
    assert pr.get(101).emby_status == "in_library"
    assert pr.get(101).emby_item_id == "e1"
    assert pr.get(102).emby_status == "none"


def test_sync_writes_checked_at(db):
    """E-6：必须记录核对时间，否则无法判断缓存新旧。"""
    seed(db, 101, "START-624")
    syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()

    from bt169.repo.posts import PostRepo
    assert PostRepo(db).get(101).emby_checked is not None


def test_sync_error_does_not_raise(db):
    """★ E-7：Emby 不可达时**不抛异常**，返回带 error 的结果。

    浏览路径绝不能因为 Emby 挂了而失败。
    """
    seed(db, 101, "START-624")
    r = syncer(db, FakeHTTP(raise_exc=OSError("连接被拒"))).sync()

    assert not r.ok
    assert "连接被拒" in r.error
    # 帖子仍在，状态未被误改
    from bt169.repo.posts import PostRepo
    assert PostRepo(db).get(101).emby_status is None


def test_sync_error_leaves_previous_status_intact(db):
    """★ 同步失败不能把**已有的**入库标记抹掉。

    抹掉会让用户以为片子被删了。缓存宁可过期也不要错误地清空。
    """
    from bt169.repo.posts import PostRepo
    seed(db, 101, "START-624")
    pr = PostRepo(db)
    syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()
    assert pr.get(101).emby_status == "in_library"

    # 第二次同步失败
    r = syncer(db, FakeHTTP(raise_exc=OSError("挂了"))).sync()
    assert not r.ok
    assert pr.get(101).emby_status == "in_library", "失败不该抹掉已有标记"


def test_sync_no_longer_in_library_clears_flag(db):
    """★ 反向也要对：片子从 Emby 删掉后，标记必须消失。

    只写不删的话，删掉的片子会永远显示「已入库」。
    """
    from bt169.repo.posts import PostRepo
    seed(db, 101, "START-624")
    pr = PostRepo(db)

    syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()
    assert pr.get(101).emby_status == "in_library"

    syncer(db, FakeHTTP(body=items(("e2", "别的片")))).sync()
    assert pr.get(101).emby_status == "none"
    assert pr.get(101).emby_item_id is None


def test_sync_skips_posts_without_code(db):
    seed(db, 101, None)
    r = syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()
    assert r.checked == 0


def test_sync_matches_case_insensitively(db):
    """★ Emby 侧是小写番号时也要命中。

    extract_codes 已经统一 upper，但这条钉住「比对时也用 upper」——
    否则 posts.code 若是小写就永远匹配不上。
    """
    from bt169.repo.posts import PostRepo
    seed(db, 101, "start-624")            # 本地小写
    syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()
    assert PostRepo(db).get(101).emby_status == "in_library"


def test_sync_from_path_only(db):
    """番号只在 Path 里时也要命中。"""
    from bt169.repo.posts import PostRepo
    seed(db, 101, "START-624")
    rows = json.dumps({"Items": [
        {"Id": "e1", "Name": "无番号", "Path": "/m/@START-624_[4K].mkv"}
    ], "TotalRecordCount": 1})
    syncer(db, FakeHTTP(body=rows)).sync()
    assert PostRepo(db).get(101).emby_status == "in_library"


def test_sync_prefix_not_confused(db):
    """E-5 端到端：本地 START-62 不得因为 Emby 有 START-624 而标记已入库。"""
    from bt169.repo.posts import PostRepo
    seed(db, 101, "START-62")
    syncer(db, FakeHTTP(body=items(("e1", "START-624")))).sync()
    assert PostRepo(db).get(101).emby_status == "none"


def test_sync_empty_library_marks_all_none(db):
    from bt169.repo.posts import PostRepo
    seed(db, 101, "START-624")
    r = syncer(db, FakeHTTP(body=items())).sync()
    assert r.ok and r.library_items == 0 and r.in_library == 0
    assert PostRepo(db).get(101).emby_status == "none"


def test_sync_result_to_dict():
    assert SyncResult(checked=3, in_library=2).to_dict() == {
        "checked": 3, "in_library": 2, "library_items": 0, "ok": True,
        "error": None,
    }


def test_get_raw_accepts_top_level_array():
    """★ ``/Library/VirtualFolders`` 返回**顶层数组**，``/Items`` 返回对象。

    第一版把解析写在只返回 dict 的 ``_get`` 里，库列表直接报
    「响应结构异常」——实测才发现。这条钉住两种形状都能解析。
    """
    body = json.dumps([{"Name": "电影", "ItemId": "lib1"}])
    c = EmbyClient(url="http://e", api_key="k", http=FakeHTTP(body=body))
    assert c._get_raw("/Library/VirtualFolders", {}) == [
        {"Name": "电影", "ItemId": "lib1"}
    ]


def test_get_still_requires_object():
    """但 ``_get``（用于 /Items）仍要求对象——数组说明端点用错了。"""
    body = json.dumps([1, 2, 3])
    c = EmbyClient(url="http://e", api_key="k", http=FakeHTTP(body=body))
    with pytest.raises(EmbyError):
        c._get("/Items", {})


# ---------------------------------------------------------------- 定时同步


def test_scheduler_tick_swallows_not_configured():
    """★ 没配 Emby 时定时器静默跳过，不报错。"""
    def build():
        raise EmbyNotConfigured("没配")

    assert EmbyScheduler(build_syncer=build).tick() is None


def test_scheduler_tick_swallows_sync_exception():
    """★ 后台线程里异常逃出去会静默杀死线程，从此再无标记。必须吞掉。"""
    class Boom:
        def sync(self):
            raise RuntimeError("炸了")

    r = EmbyScheduler(build_syncer=lambda: Boom()).tick()
    assert r is not None and not r.ok
    assert "炸了" in r.error


def test_scheduler_tick_swallows_build_exception():
    def build():
        raise RuntimeError("构造炸了")

    r = EmbyScheduler(build_syncer=build).tick()
    assert r is not None and "构造炸了" in r.error


def test_scheduler_tick_none_syncer():
    assert EmbyScheduler(build_syncer=lambda: None).tick() is None


def test_scheduler_run_respects_stop_event():
    """★ 停止信号必须能立刻打断等待，否则关服务要等满一个 interval。"""
    import threading
    import time

    calls = []
    stop = threading.Event()

    class S:
        def sync(self):
            calls.append(1)
            return SyncResult()

    sch = EmbyScheduler(build_syncer=lambda: S(), interval=3600,
                        stop_event=stop)
    t = threading.Thread(target=sch.run, daemon=True)
    t.start()
    time.sleep(0.15)          # 够跑一次 tick
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "stop_event 未能打断等待"
    assert len(calls) >= 1


def test_scheduler_interval_not_drifting():
    """★ 间隔按「下次该跑的时刻」算，不把同步耗时累加进去。

    固定 ``sleep(interval)`` 会让实际间隔变成 interval + 耗时，越漂越远。
    """
    import threading

    fake_now = [0.0]
    waits = []

    class StopAfterTwo:
        def __init__(self):
            self.n = 0

        def is_set(self):
            self.n += 1
            return self.n > 4

        def set(self):
            pass

        def wait(self, t):
            waits.append(t)
            fake_now[0] += t

    class S:
        def sync(self):
            fake_now[0] += 100.0   # 模拟同步耗时 100 秒
            return SyncResult()

    sch = EmbyScheduler(build_syncer=lambda: S(), interval=900,
                        stop_event=StopAfterTwo(),
                        clock=lambda: fake_now[0])
    sch.run()

    # 第一次 tick 后 now=100，deadline=100+900=1000；
    # 若用固定 sleep(900)，第二次 tick 会发生在 now=1000 而 deadline 应为 1000
    # → 实际等待总时长应远小于 900（因为已经用掉 100 秒）
    assert sum(waits) < 900, f"等待 {sum(waits)} 秒，说明间隔漂移了"


# ---------------------------------------------------------------- 代理环境


def test_default_http_ignores_env_proxy():
    """★ Emby 几乎总在**局域网**，不能被 ``http_proxy`` 劫走。

    httpx 默认 ``trust_env=True``，会读 ``http_proxy``/``https_proxy``。
    把这些地址丢给互联网代理（Clash/V2Ray）只会得到 502。

    实测（本机开着 ``http_proxy=http://127.0.0.1:7890``）：
        trust_env=True  → HTTP 502       ← 看起来像 Emby 挂了
        trust_env=False → ConnectError   ← 真实的「连不上」
    """
    from bt169.emby import default_http

    c = default_http()
    try:
        assert c.trust_env is False, "必须 trust_env=False，否则局域网地址会被代理劫走"
    finally:
        c.close()


def test_default_http_has_timeout():
    from bt169.emby import default_http

    c = default_http(timeout=7.5)
    try:
        assert c.timeout.read == 7.5
    finally:
        c.close()


def test_scheduler_delay_first_defers_initial_tick():
    """★ ``delay_first=True``：首次 tick 必须**等一个 interval**。

    这是会话续期的安全前提——续期消耗登录额度（5 次 / 900 秒，按 IP），
    而重启服务是常见操作。若启动即续期，反复重启就把额度烧光了。
    """
    fake_now = [0.0]
    calls = []

    class StopAt:
        """★ 按**虚拟时钟**停止，而不是按 is_set 调用次数。

        ``_wait_until`` 内部是 ``min(remaining, 5.0)`` 轮询，is_set 会被
        调用上百次；按次数计数的假事件会过早变 True（假失败）。
        """

        def __init__(self, limit):
            self.limit = limit

        def is_set(self):
            return fake_now[0] >= self.limit

        def set(self):
            pass

        def wait(self, t):
            fake_now[0] += t

    class S:
        def sync(self):
            calls.append(fake_now[0])
            return SyncResult()

    sch = EmbyScheduler(build_syncer=lambda: S(), interval=900,
                        stop_event=StopAt(1800),
                        clock=lambda: fake_now[0], delay_first=True)
    sch.run()

    assert calls, "应该至少跑过一次"
    assert calls[0] >= 900, f"首次 tick 发生在 t={calls[0]}，早于一个 interval"


def test_scheduler_default_ticks_immediately():
    """默认（``delay_first=False``）首次 tick 立即跑——Emby 同步不烧额度。"""
    fake_now = [0.0]
    calls = []

    class StopAt:
        def __init__(self, limit):
            self.limit = limit

        def is_set(self):
            return fake_now[0] >= self.limit

        def set(self):
            pass

        def wait(self, t):
            fake_now[0] += t

    class S:
        def sync(self):
            calls.append(fake_now[0])
            return SyncResult()

    sch = EmbyScheduler(build_syncer=lambda: S(), interval=900,
                        stop_event=StopAt(1800),
                        clock=lambda: fake_now[0])
    sch.run()

    assert calls[0] == 0.0, f"默认应立即 tick，实际 t={calls[0]}"


def test_scheduler_delay_first_stop_interrupts_deferred_wait():
    """★ 推迟等待期间收到停止信号，必须立刻退出而不是空转一个 interval。"""
    import threading
    import time

    calls = []
    stop = threading.Event()

    class S:
        def sync(self):
            calls.append(1)
            return SyncResult()

    sch = EmbyScheduler(build_syncer=lambda: S(), interval=3600,
                        stop_event=stop, delay_first=True)
    t = threading.Thread(target=sch.run, daemon=True)
    t.start()
    time.sleep(0.15)
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "推迟等待未能被 stop_event 打断"
    assert calls == [], "停止信号后不该再 tick"


# ------------------------------------------------------------ 用户解析（库过滤）


def users(*specs):
    """``/Users`` 返回**顶层数组**，条目含 Name / Id。"""
    return json.dumps([{"Name": n, "Id": i} for n, i in specs])


def test_resolve_user_id_exact_match():
    """★ 精确匹配用户名 → 拿到 Id。

    真实 Emby 的 ``/Users`` 条目形如 ``{"Name":"muse","Id":"68f6…"}``。
    """
    http = FakeHTTP(body=users(("mario", "u1"), ("muse", "u2"), ("xy", "u3")))
    c = EmbyClient(url="http://e", api_key="k", http=http)

    assert c.resolve_user_id("muse") == "u2"
    assert http.calls[0][0].endswith("/Users")


def test_resolve_user_id_is_case_sensitive():
    """★ 大小写必须敏感（用户明确要求）。

    Emby 里 ``muse`` 与 ``Muse`` 是两个不同的账号，各自权限不同。
    大小写不敏感会静默匹配到**别人的**权限集合——这里宁可报错。
    """
    http = FakeHTTP(body=users(("muse", "u2")))
    c = EmbyClient(url="http://e", api_key="k", http=http)

    with pytest.raises(EmbyError):
        c.resolve_user_id("Muse")


def test_resolve_user_id_not_found_raises():
    http = FakeHTTP(body=users(("muse", "u2")))
    c = EmbyClient(url="http://e", api_key="k", http=http)

    with pytest.raises(EmbyError) as ei:
        c.resolve_user_id("不存在的人")
    assert "不存在的人" in str(ei.value)


def test_resolve_user_id_empty_username_raises():
    """空用户名不该发请求——调用方应先判断，这里兜底。"""
    http = FakeHTTP(body=users(("muse", "u2")))
    c = EmbyClient(url="http://e", api_key="k", http=http)

    with pytest.raises(EmbyError):
        c.resolve_user_id("")
    assert http.calls == []
