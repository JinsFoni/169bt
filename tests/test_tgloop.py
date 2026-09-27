"""tgloop：Telethon 专用事件循环线程。

全部离线、不碰网络。核心回归：真实故障「The asyncio event loop must
not change after connection」——FastAPI 同步路由的两次请求可能落在
不同线程，Telethon 连接后禁止换 loop，因此所有操作必须递交到同一个
常驻 loop。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from bt169 import tgloop


@pytest.fixture(autouse=True)
def _reset_loop():
    """每个测试用独立的 loop 线程，避免用例间串状态。"""
    tgloop._loop = None
    yield
    if tgloop._loop is not None and not tgloop._loop.is_closed():
        tgloop._loop.call_soon_threadsafe(tgloop._loop.stop)


# ---------------------------------------------------------------- 基础


def test_run_executes_coroutine_on_dedicated_loop():
    """run() 执行协程并返回结果；loop 与调用线程解耦。"""
    async def coro():
        return asyncio.get_running_loop(), threading.current_thread().name

    loop, thread = tgloop.run(coro())
    assert loop is tgloop._loop
    assert thread == "tg-mtproto-loop"


def test_run_reuses_same_loop_across_caller_threads():
    """★ 核心回归：不同调用线程 → 同一个 loop。

    真实故障即「两个请求两个线程两个 loop」；这里模拟 FastAPI 线程池。
    """
    async def loop_id():
        return id(asyncio.get_running_loop())

    seen = set()
    threads = []
    results = {}

    def work(i):
        results[i] = tgloop.run(loop_id())

    for i in range(4):
        t = threading.Thread(target=work, args=(i,), name=f"caller-{i}")
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    seen = set(results.values())
    assert len(seen) == 1, f"loop 在调用线程间发生了变化: {seen}"


def test_run_propagates_exception():
    """协程里的异常原样穿透到调用线程。"""

    async def boom():
        raise ValueError("tg boom")

    with pytest.raises(ValueError, match="tg boom"):
        tgloop.run(boom())


def test_run_timeout_cancels_and_raises():
    """超时 → TimeoutError（请求取消，不让挂死协程拖住调用方）。"""
    import bt169.tgloop as m

    async def slow():
        await asyncio.sleep(30)

    old = m.DEFAULT_TIMEOUT
    m.DEFAULT_TIMEOUT = 0.05
    try:
        with pytest.raises(TimeoutError):
            tgloop.run(slow())
    finally:
        m.DEFAULT_TIMEOUT = old


def test_maybe_await_handles_all_shapes():
    """协程 / future / 普通值三种形态统一。"""

    async def main():
        assert await tgloop.maybe_await(_coro()) == 1
        assert await tgloop.maybe_await(asyncio.ensure_future(_coro())) == 1
        assert await tgloop.maybe_await(1) == 1          # 假客户端直接回值
        assert await tgloop.maybe_await(None) is None

    async def _coro():
        return 1

    tgloop.run(main())


# ---------------------------------------------------------------- 集成

class FakeTelethon:
    """模拟「连接后 loop 绑定」的 Telethon 客户端（复刻 1.45 行为）。"""

    def __init__(self):
        self._loop = None
        self.sent = []

    async def connect(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop != loop:
            raise RuntimeError(
                "The asyncio event loop must not change after connection")

    async def send_message(self, entity, text):
        assert asyncio.get_running_loop() is self._loop
        self.sent.append((entity, text))
        return type("M", (), {"id": len(self.sent)})()


def test_mtp_sender_across_threads():
    """★ 端到端回归：MtpSender.send 从不同线程调用不再炸 loop 校验。"""
    from bt169.mtpproto import MtpSender

    fake = FakeTelethon()
    sender = MtpSender(client=fake, target="@bot")

    ids = {}

    def work(i):
        ids[i] = sender.send(f"ed2k://|file|X-{i}|1|AB|/")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(ids) == [0, 1, 2]
    assert len(fake.sent) == 3
