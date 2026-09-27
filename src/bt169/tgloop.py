"""Telethon 专用事件循环线程（跨线程复用 MTProto 连接的基础设施）。

背景（2026-09，真实故障）：FastAPI 的**同步**路由跑在 anyio 工作线程
池里，同一逻辑端点的两次请求可能落在**不同线程**。Telethon 客户端在
首次 ``connect()`` 时把 ``self._loop`` 绑定到*当时线程*的事件循环；
下一个请求换了线程 → 换了一个新 loop → 再 ``send`` 即抛：

    RuntimeError: The asyncio event loop must not change after connection
    （用户看到："MTProto 发送失败：The asyncio event loop must not …"）

修复：进程内起**一个守护线程**跑**一个常驻事件循环**，所有 Telethon
操作（转发发送 / 登录向导 / 断开清理）都经 :func:`run` 递交到它执行。
调用线程是谁无所谓——loop 永不改变，连接得以跨请求复用，与
:func:`bt169.mtpproto.get_sender` 的单例设计正好配套。

两个出口：

- :func:`run` —— 把协程递交到专用 loop 并阻塞等结果（带超时防挂死）。
- :func:`maybe_await` —— telethon.sync 的包装在「loop 正在运行」时返回
  协程对象；未导入 sync 时方法本身就是协程；测试假客户端直接返回值。
  三种形态统一 await，调用方无需关心当前是哪种。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

__all__ = ["DEFAULT_TIMEOUT", "run", "maybe_await"]

#: 单次 Telegram 操作的最长等待（秒）。转发单条 / 登录单步正常都在
#: 秒级完成；超时只防「网络挂死拖垮 API 工作线程」。
#: ★ 运行期可被 monkeypatch（测试把它调小）。
DEFAULT_TIMEOUT: float = 60.0

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """懒启动专用 loop 线程；loop 意外关闭时重建（尽力而为）。"""
    global _loop
    with _lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever,
                name="tg-mtproto-loop",
                daemon=True,      # 不挡进程退出
            ).start()
        return _loop


def run(coro: Any, *, timeout: float | None = None) -> Any:
    """在专用 loop 上执行 ``coro``，阻塞当前线程直到完成。

    Raises:
        TimeoutError: 超过 ``timeout``（默认 :data:`DEFAULT_TIMEOUT`）未完成。
            超时会向协程请求取消；远端是否实际发出不由这里保证——
            上层的幂等（``tg_sent_at``）会兜住重试造成的重复发送。
        其余异常原样穿透（如 ``FloodWaitError``），由调用方翻译。
    """
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(DEFAULT_TIMEOUT if timeout is None else timeout)
    except TimeoutError:
        # Python 3.11+ 里 concurrent.futures.TimeoutError 就是内建 TimeoutError
        fut.cancel()
        raise


async def maybe_await(value: Any) -> Any:
    """统一 await 三种返回形态（见模块 docstring）。"""
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value
