"""MTProto 传输层（Telethon，方案 A）。

全部离线：Telethon 客户端用 FakeMtp 假客户端替换，
不打真实 Telegram 网络。覆盖：

- 未配置 api_id/api_hash/session → MtpNotConfigured（→ 400 tg_not_configured）
- 发送内容 = 纯 ed2k 链接（T-1）
- FloodWaitError → RateLimited（带 retry_after）
- 其他 Telethon 异常 → TelegramError（→ 502）
- 幂等 / 串行间隔等上层行为复用 telegram.py，不在此重复
"""

from __future__ import annotations

import pytest

from bt169 import config
from bt169.mtpproto import MtpNotConfigured, MtpSender, build_sender
from bt169.repo.settings import SettingsRepo
from bt169.telegram import RateLimited, TelegramError

# ---------------------------------------------------------------- 假客户端


class FakeMtp:
    """替身 Telethon 客户端：记录 send_message 调用。"""

    def __init__(self, *, exc=None, message_id=42):
        self.calls = []
        self.exc = exc
        self.message_id = message_id
        self.connected = False
        self.disconnected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.disconnected = True

    def send_message(self, entity, text):
        self.calls.append((entity, text))
        if self.exc is not None:
            raise self.exc
        # 真客户端返回 Message 对象；上层只用 id
        class M:
            id = self.message_id
        return M()


@pytest.fixture()
def sender_with(db, box):
    """已配置 MTP 的 sender 工厂：返回 (MtpSender, FakeMtp)。"""
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "api_id"), "12345")
    sr.put(config.section_key("tg", "api_hash"), "deadbeef")
    sr.put(config.section_key("tg", "session"), "1BaBhK0Q4Q04nPqrfYHkC5rDWXk")
    sr.put(config.section_key("tg", "target"), "@nan_share_bot")

    def make(fake=None):
        fake = fake or FakeMtp()
        sender = MtpSender(
            client=fake,
            target="@nan_share_bot",
        )
        return sender, fake

    return make


# ---------------------------------------------------------------- 单元


def test_build_sender_not_configured(db, box):
    """什么都没配 → MtpNotConfigured。"""
    with pytest.raises(MtpNotConfigured):
        build_sender(SettingsRepo(db, box))


def test_build_sender_partial(db, box):
    """只配了 api_id → 同样视为未配置（三要素缺一不可）。"""
    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "api_id"), "12345")
    with pytest.raises(MtpNotConfigured):
        build_sender(sr)


def test_build_sender_wires_settings(db, box, monkeypatch):
    """三要素齐 → 用设置值构造 Telethon 客户端 + target。"""
    import bt169.mtpproto as m

    sr = SettingsRepo(db, box)
    sr.put(config.section_key("tg", "api_id"), "12345")
    sr.put(config.section_key("tg", "api_hash"), "deadbeef")
    sr.put(config.section_key("tg", "session"), "1BaBhK0Q4Q04nPqrfYHkC5rDWXk")

    captured = {}

    def fake_ctor(session, api_id, api_hash, proxy=None):
        captured.update(session=session, api_id=api_id, api_hash=api_hash)
        return FakeMtp()

    monkeypatch.setattr(m, "TelegramClient", fake_ctor)
    monkeypatch.setattr(m, "StringSession", lambda s: s)
    sender = build_sender(sr)
    assert captured == {
        "session": "1BaBhK0Q4Q04nPqrfYHkC5rDWXk",
        "api_id": 12345,
        "api_hash": "deadbeef",
    }
    assert sender.target == ""  # 未配 target → 空串占位（构造期不报错）


def test_send_sends_plain_ed2k(sender_with):
    """发送内容就是纯链接，不包任何前缀（T-1）。"""
    sender, fake = sender_with()
    mid = sender.send("ed2k://|file|ABC-101.mkv|852000000|AABBCCDD|/")
    assert mid == 42
    assert fake.calls == [("@nan_share_bot",
                           "ed2k://|file|ABC-101.mkv|852000000|AABBCCDD|/")]


def test_send_connects_first(sender_with):
    """★ Telethon 不会自动连：send 前必须 connect（幂等）。"""
    sender, fake = sender_with()
    sender.send("hello")
    assert fake.connected is True


def test_send_requires_target(sender_with):
    """target 为空 → MtpNotConfigured（转发目标必须有）。"""
    sender, _ = sender_with()
    sender.target = ""
    with pytest.raises(MtpNotConfigured):
        sender.send("ed2k://|file|x|1|AB|/")


def test_flood_wait_maps_to_rate_limited(sender_with):
    """FloodWaitError → RateLimited，retry_after = e.seconds。"""
    from telethon.errors import FloodWaitError

    sender, _ = sender_with()
    fake_exc = FloodWaitError(request=None, capture=31)
    s, _ = sender_with()
    s.client = FakeMtp(exc=fake_exc)
    with pytest.raises(RateLimited) as ei:
        s.send("ed2k://|file|x|1|AB|/")
    assert ei.value.retry_after == 31


def test_other_error_maps_to_telegram_error(sender_with):
    """其余 Telethon 异常 → TelegramError（502 路径）。"""
    s, _ = sender_with()
    s.client = FakeMtp(exc=ValueError("boom"))
    with pytest.raises(TelegramError):
        s.send("ed2k://|file|x|1|AB|/")
