"""设置页内 MTProto 登录向导（MtpLoginManager）。

全部离线：signer 用假实现。覆盖：

- start → verify → session 全流程
- 两步验证分支（need_password → password）
- 验证码错 → MtpLoginError（可重试）
- 进程级冷却：start 失败/成功后 30 秒内再点 → RateLimited
- 超时：5 分钟未完成 → LoginExpired
- cancel 清理
"""

from __future__ import annotations

import pytest

from bt169.tglogin import (
    LoginExpired,
    MtpLoginError,
    MtpLoginManager,
)
from bt169.telegram import RateLimited


class FakeSigner:
    """替身 signer：模拟 Telethon 登录三段流程。"""

    def __init__(self, *, need_password=False, code_ok=True, **kw):
        self.creds = kw
        self.send_code_calls = []
        self.need_password = need_password
        self.code_ok = code_ok
        self.signed_in = False
        self.cancelled = False

    def send_code(self, phone):
        self.send_code_calls.append(phone)

    def sign_in(self, code):
        if not self.code_ok:
            raise MtpLoginError("tg_code_invalid", "验证码错误")
        if self.need_password:
            from telethon.errors import SessionPasswordNeededError
            raise SessionPasswordNeededError(request=None)
        self.signed_in = True

    def sign_in_password(self, password):
        if password != "right":
            raise MtpLoginError("tg_password_invalid", "两步验证密码错误")
        self.signed_in = True

    def session(self):
        return "SESSION-OK" if self.signed_in else ""

    def cancel(self):
        self.cancelled = True


@pytest.fixture()
def holder():
    """工厂替身：每次返回可配置的 FakeSigner。"""
    class Holder:
        def __init__(self):
            self.last = None

        def make(self, **kw):
            self.last = FakeSigner(**kw)
            return self.last
    return Holder()


def test_full_flow_without_password(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    assert m.start("+8613800000000", api_id=12345, api_hash="h") == {"ok": True}
    out = m.verify("12345")
    assert out == {"ok": True, "session": "SESSION-OK"}
    # 成功后状态清空
    assert m.active is False


def test_flow_with_two_step_password(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    m.start("+86138", api_id=12345, api_hash="h")
    holder.last.need_password = True
    assert m.verify("12345") == {"need_password": True}
    assert m.active is True  # 等密码
    out = m.password("right")
    assert out == {"ok": True, "session": "SESSION-OK"}
    assert m.active is False


def test_wrong_password_then_retry(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    m.start("+86138", api_id=12345, api_hash="h")
    holder.last.need_password = True
    m.verify("1")
    with pytest.raises(MtpLoginError) as ei:
        m.password("wrong")
    assert ei.value.code == "tg_password_invalid"
    # 密码错后仍处于 need_password 态，可重试
    assert m.active is True
    assert m.password("right")["ok"] is True


def test_wrong_code_maps_to_login_error(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    m.start("+86138", api_id=12345, api_hash="h")
    holder.last.code_ok = False
    with pytest.raises(MtpLoginError) as ei:
        m.verify("bad")
    assert ei.value.code == "tg_code_invalid"
    # 验证码错后仍可重试
    holder.last.code_ok = True
    assert m.verify("12345")["ok"] is True


def test_start_cooldown_blocks_rapid_restart(holder):
    """★ 用户要求的保护：start 之后（无论成败）30 秒内再点 → RateLimited。"""
    m = MtpLoginManager(holder.make, start_cooldown=30)
    m.start("+86138", api_id=12345, api_hash="h")
    with pytest.raises(RateLimited) as ei:
        m.start("+86139", api_id=12345, api_hash="h")
    assert 0 < ei.value.retry_after <= 30


def test_verify_without_start_is_error(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    with pytest.raises(MtpLoginError) as ei:
        m.verify("1")
    assert ei.value.code == "tg_login_expired"


def test_expiry_after_timeout(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0, timeout=0.0)
    m.start("+86138", api_id=12345, api_hash="h")
    with pytest.raises(LoginExpired):
        m.verify("1")
    assert m.active is False


def test_cancel_cleans_up(holder):
    m = MtpLoginManager(holder.make, start_cooldown=0)
    m.start("+86138", api_id=12345, api_hash="h")
    m.cancel()
    assert m.active is False
    assert holder.last.cancelled is True


def test_start_replaces_previous_attempt(holder):
    """重新发码 = 放弃上一次，旧 signer 必须断开（不泄漏连接）。"""
    m = MtpLoginManager(holder.make, start_cooldown=0)
    m.start("+86138", api_id=12345, api_hash="h")
    first = holder.last
    m.start("+86139", api_id=12345, api_hash="h")
    assert first.cancelled is True
    assert holder.last is not first
