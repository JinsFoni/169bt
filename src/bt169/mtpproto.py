"""MTProto 传输层（方案 A）：用**用户账号**把 ed2k 发给目标 bot。

背景（探针结论，2026-09）：Telegram 平台规定 bot 之间默认收不到对方消息
（``USER_BOT_TO_BOT_DISABLED``）。NS Bot 未开启 Bot-to-Bot 模式，因此
Bot API 路径（原 ``TelegramClient``）永远无法把链接送进 NS Bot 背后的服务。
用户账号（MTProto）发送与人工手动发送同构，服务必然按真人消息处理。

设计要点：

- **传输层与业务层分离**：转发幂等（``tg_sent_at``）、串行间隔、限流停手
  全部在 :mod:`bt169.telegram` 的业务层实现，这里只提供
  ``send(text) -> message_id``。替换传输层 = 换一个 ``send`` 实现。
- **凭据安全**：``api_hash`` 与 StringSession 等于账号本身，落库走
  SecretBox 加密（键名最后一段进 ``SECRET_FIELDS``）。
- **绝不抛业务外异常**：Telethon 异常统一翻译为
  ``RateLimited`` / ``TelegramError``（E-7 边界）。
- **懒加载单例**：长连接复用，进程内只连一次；发送失败不关闭连接
  （短间隔重发不必重握手）。
- **所有 Telethon 调用跑在专用 loop 线程**（:mod:`bt169.tgloop`）：
  FastAPI 同步路由的任何两次请求可能落在不同线程，而 Telethon 在首次
  ``connect()`` 后禁止换事件循环（否则 "The asyncio event loop must not
  change after connection"）。递交到常驻 loop 线程后，连接真正可跨
  请求复用，与 :func:`get_sender` 的单例设计配套。
"""

from __future__ import annotations

import threading
from typing import Any

import telethon.sync  # noqa: F401  ★ 缺它协程永不执行，见 tglogin.py 同款注释
from telethon import TelegramClient
from telethon.sessions import StringSession

from bt169.telegram import TelegramNotConfigured
from bt169.tgloop import maybe_await, run

__all__ = ["MtpNotConfigured", "MtpSender", "build_sender", "get_sender"]

# 延迟导入：telethon 较重，测试与未启用 MTP 的进程不必付出 import 代价。


class MtpNotConfigured(TelegramNotConfigured):
    """未配置 MTProto（api_id / api_hash / session / target 缺失）。

    继承 :class:`bt169.telegram.TelegramNotConfigured`：路由层 ``_fail``
    已把它映射为 400 + ``tg_not_configured``，前端引导去设置页，
    无需新增错误码。
    """


class MtpSender:
    """Telethon 客户端的同步包装：``send(text) -> message_id``。

    与 :class:`bt169.telegram.TelegramClient` 同构（duck typing），
    业务层 ``forward_post`` / ``forward_many`` 无需感知差异。
    """

    def __init__(self, client: Any, target: str) -> None:
        self.client = client
        self.target = target.strip()

    def send(self, text: str) -> int:
        """发送一条文本到 target，返回 message_id。

        ★ 全程在专用 loop 线程执行（:mod:`bt169.tgloop`）：FastAPI 同步
          路由每次请求可能在不同线程，而 Telethon 禁止连接后换事件循环。

        Raises:
            RateLimited: FloodWait——retry_after 用服务端给定的秒数。
            TelegramError: 其他失败（含网络异常、超时）。
            MtpNotConfigured: target 为空。
        """
        from bt169.telegram import RateLimited, TelegramError

        if not self.target:
            raise MtpNotConfigured("尚未配置转发目标（tg.target）")

        from telethon import errors

        try:
            async def _send() -> int:
                # ★ Telethon 不会自动重连：客户端处于断开状态（新建/上次断开）
                #   时直接 send 会报 "Cannot send requests while disconnected"。
                #   先 connect——已连接时是幂等空操作。
                await maybe_await(self.client.connect())
                msg = await maybe_await(self.client.send_message(self.target, text))
                return int(getattr(msg, "id", 0) or 0)

            return run(_send())
        except errors.FloodWaitError as exc:
            raise RateLimited(
                f"Telegram 限流，需等待 {exc.seconds} 秒",
                int(exc.seconds or 0),
            ) from exc
        except Exception as exc:  # noqa: BLE001 — MTP 层异常种类繁多，统一翻译
            raise TelegramError(f"MTProto 发送失败：{exc}") from exc

    def test(self) -> None:
        """设置面板「测试」按钮：发一条带时间戳的探针消息。"""
        from datetime import datetime

        stamp = datetime.now().strftime("%H:%M:%S")
        self.send(f"169bt MTProto 测试消息（{stamp}）。")


def build_sender(settings: Any) -> MtpSender:
    """从设置构造 :class:`MtpSender`（每次新建 Telethon 客户端）。

    供单条转发与测试使用；常驻服务请用 :func:`get_sender`（单例）。
    """
    from bt169.config import section_key
    from bt169.netproxy import telethon_proxy

    api_id = (settings.get(section_key("tg", "api_id")) or "").strip()
    api_hash = (settings.get(section_key("tg", "api_hash")) or "").strip()
    session = (settings.get(section_key("tg", "session")) or "").strip()
    target = (settings.get(section_key("tg", "target")) or "").strip()

    # 三要素缺一不可；target 允许为空（构造期），发送时才报
    if not api_id or not api_hash or not session:
        raise MtpNotConfigured(
            "尚未配置 MTProto：需要 api_id、api_hash 与已登录的 session"
            "（运行 bt169 tg-login 生成）"
        )
    client = TelegramClient(
        StringSession(session), int(api_id), api_hash,
        proxy=telethon_proxy(settings),
    )
    return MtpSender(client, target)


_lock = threading.Lock()
_sender: MtpSender | None = None
_sender_sig: tuple[str, str, str, str] | None = None


def get_sender(settings: Any) -> MtpSender:
    """进程内单例：凭据（api_id/api_hash/session/target）与代理不变就复用连接。

    设置改动 → 换凭据签名 → 重建客户端。**不抛连接异常**——连接推迟到
    首次 ``send``（Telethon 在 send 前会自动 connect）。
    """
    global _sender, _sender_sig

    from bt169.config import section_key
    from bt169.netproxy import telethon_proxy

    api_id = (settings.get(section_key("tg", "api_id")) or "").strip()
    api_hash = (settings.get(section_key("tg", "api_hash")) or "").strip()
    session = (settings.get(section_key("tg", "session")) or "").strip()
    target = (settings.get(section_key("tg", "target")) or "").strip()

    if not api_id or not api_hash or not session:
        raise MtpNotConfigured(
            "尚未配置 MTProto：需要 api_id、api_hash 与已登录的 session"
            "（运行 bt169 tg-login 生成）"
        )

    proxy = telethon_proxy(settings)
    sig = (api_id, api_hash, session, target, repr(proxy))
    with _lock:
        if _sender is None or _sender_sig != sig:
            # 先丢旧连接（尽力而为；失败不影响新连接建立）。
            # ★ 也必须递交到专用 loop——disconnect 是协程，跨线程直接调
            #   会踩同一个事件循环变更问题。
            if _sender is not None:
                try:
                    run(_sender.client.disconnect())
                except Exception:  # noqa: BLE001
                    pass
            client = TelegramClient(
                StringSession(session), int(api_id), api_hash, proxy=proxy
            )
            _sender = MtpSender(client, target)
            _sender_sig = sig
        return _sender
