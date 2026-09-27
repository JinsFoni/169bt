"""设置页内 MTProto 登录向导状态机。

三步流程：``start(手机号)`` → ``verify(验证码)`` → [``password(两步验证)``]。

设计要点：

- **进程内单例状态**：登录上下文（临时 Telethon 客户端 + phone_code_hash）
  只存内存，5 分钟未完成自动过期。服务重启 = 重新走流程（Telegram 的
  验证码本身也是短时效的，语义一致）。
- **平台限频保护（用户明确要求）**：发验证码是 Telegram 严格限频的操作，
  短时间反复请求会触发越来越长的 FLOOD_WAIT。因此 ``start`` 无论成败，
  成功后再点都会被进程级冷却挡住（默认 30 秒），并把剩余秒数透传给前端
  禁用按钮。
- **与转发链路隔离**：登录用独立的临时客户端，成功落库后由
  ``mtpproto.get_sender`` 的凭据签名机制自然感知并重建转发连接。
- **绝不抛业务外异常**：所有失败统一为 ``MtpLoginError(code, message)``
  或 ``RateLimited`` / ``LoginExpired``，路由层据此返回结构化错误。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from bt169 import tgloop
from bt169.telegram import RateLimited

__all__ = [
    "LoginExpired",
    "MtpLoginError",
    "MtpLoginManager",
    "TelethonSigner",
    "get_login_manager",
]

#: 发码冷却秒数（用户要求：失败后不能立刻再点）。
DEFAULT_START_COOLDOWN = 30.0

#: 登录流程超时（Telegram 验证码约 5 分钟有效，与其对齐）。
DEFAULT_TIMEOUT = 300.0


class MtpLoginError(Exception):
    """登录流程错误。``code`` 供前端分支提示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LoginExpired(MtpLoginError):
    """登录流程已超时/不存在，需从头开始。"""

    def __init__(self) -> None:
        super().__init__("tg_login_expired", "登录会话已过期，请重新发送验证码")


class TelethonSigner:
    """真 Telethon 实现的三段登录器（测试中替换）。

    ★ Telethon 的 ``TelegramClient`` **构造时就要求** api_id/api_hash
    （报错 ``missing 2 required positional arguments``），因此凭据由
    ``MtpLoginManager.start`` 透传给工厂，再进入这里。
    """

    def __init__(self, *, api_id: int, api_hash: str) -> None:
        # ★ 必须 import telethon.sync:它把客户端的协程方法改写为同步包装。
        #   缺了它 connect()/send_code_request() 只会创建协程对象而**永不执行**
        #   （日志里 "coroutine ... was never awaited"），接口却返回 200，
        #   验证码实际没发出去。路由跑在无事件循环的线程里，同步包装可用。
        import telethon.sync  # noqa: F401  （导入即生效）
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        # ★ start/verify/password 分属**不同的 HTTP 请求** → 可能落在
        #   线程池的不同线程 → 而连接后禁止换事件循环（同 mtpproto 的
        #   真实故障）。所有调用都递交到 tgloop 的常驻 loop 线程。
        self._loop = tgloop._ensure_loop()

        # 登录期间用空 StringSession：完成时才有可用会话
        self._client = TelegramClient(
            StringSession(), int(api_id), api_hash
        )

    def _run(self, coro: Any) -> Any:
        return tgloop.run(coro)

    def send_code(self, phone: str) -> None:
        async def _go() -> None:
            await tgloop.maybe_await(self._client.connect())
            await tgloop.maybe_await(self._client.send_code_request(phone))
        self._run(_go())

    def sign_in(self, code: str) -> None:
        self._run(tgloop.maybe_await(self._client.sign_in(code=code)))

    def sign_in_password(self, password: str) -> None:
        self._run(tgloop.maybe_await(self._client.sign_in(password=password)))

    def session(self) -> str:
        return self._client.session.save() or ""

    def cancel(self) -> None:
        try:
            self._run(tgloop.maybe_await(self._client.disconnect()))
        except Exception:  # noqa: BLE001 — 清理尽力而为
            pass


class MtpLoginManager:
    """登录状态机。线程安全（进程内唯一，路由并发访问）。"""

    def __init__(
        self,
        signer_factory: Callable[[], Any],
        *,
        start_cooldown: float = DEFAULT_START_COOLDOWN,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._make_signer = signer_factory
        self._cooldown = start_cooldown
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._signer: Any | None = None
        self._phone: str | None = None
        self._stage: str = ""          # '' | 'code' | 'password'
        self._started_at: float = 0.0
        self._last_start: float = float("-inf")

    # ------------------------------------------------------------ 查询

    @property
    def active(self) -> bool:
        return self._signer is not None

    def _expired(self) -> bool:
        return (
            self._signer is None
            or (self._clock() - self._started_at) > self._timeout
        )

    def _reset_locked(self) -> None:
        if self._signer is not None:
            self._signer.cancel()
        self._signer = None
        self._phone = None
        self._stage = ""

    # ------------------------------------------------------------ 步骤

    def start(
        self, phone: str, *, api_id: int, api_hash: str
    ) -> dict[str, Any]:
        """发送验证码。冷却期内抛 :class:`RateLimited`（剩余秒数透传）。

        ``api_id`` / ``api_hash`` 由调用方（路由）从设置读出后传入——
        Telethon 客户端构造时就要求它们，不能等 verify 阶段再给。
        """
        with self._lock:
            elapsed = self._clock() - self._last_start
            if elapsed < self._cooldown:
                raise RateLimited(
                    f"发送太频繁，请 {int(self._cooldown - elapsed) + 1} 秒后再试",
                    int(self._cooldown - elapsed) + 1,
                )
            self._last_start = self._clock()
            # 重发 = 放弃上一次（断开旧客户端，避免连接泄漏）
            self._reset_locked()
            try:
                signer = self._make_signer(api_id=api_id, api_hash=api_hash)
                signer.send_code(phone)
            except RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001 — 统一翻译
                self._reset_locked()
                raise MtpLoginError(
                    "tg_send_code_failed", _translate_send_code(exc)
                ) from exc
            self._signer = signer
            self._phone = phone
            self._stage = "code"
            self._started_at = self._clock()
            return {"ok": True}

    def verify(self, code: str) -> dict[str, Any]:
        """提交验证码。需两步验证时返回 ``{"need_password": True}``。"""
        with self._lock:
            if self._expired():
                self._reset_locked()
                raise LoginExpired()
            try:
                self._signer.sign_in(code)
            except Exception as exc:  # noqa: BLE001
                return self._classify_sign_in(exc)
            return self._finish_locked()

    def password(self, password: str) -> dict[str, Any]:
        """提交两步验证密码（仅 ``need_password`` 后有效）。"""
        with self._lock:
            if self._expired():
                self._reset_locked()
                raise LoginExpired()
            if self._stage != "password":
                raise MtpLoginError(
                    "tg_login_expired", "当前不需要两步验证，请从验证码步骤继续"
                )
            try:
                self._signer.sign_in_password(password)
            except Exception as exc:  # noqa: BLE001
                return self._classify_sign_in(exc)
            return self._finish_locked()

    def cancel(self) -> None:
        """放弃登录，断开临时客户端。"""
        with self._lock:
            self._reset_locked()

    # ------------------------------------------------------------ 内部

    def _classify_sign_in(self, exc: Exception) -> dict[str, Any]:
        """把 sign_in 抛的异常归类为结构化结果（可重试的不清状态）。"""
        from telethon import errors

        if isinstance(exc, errors.SessionPasswordNeededError):
            self._stage = "password"
            return {"need_password": True}
        if isinstance(exc, errors.PhoneCodeInvalidError):
            raise MtpLoginError("tg_code_invalid", "验证码错误") from exc
        if isinstance(exc, errors.PhoneCodeExpiredError):
            raise MtpLoginError("tg_code_expired", "验证码已过期，请重新发送") from exc
        if isinstance(exc, errors.PasswordHashInvalidError):
            raise MtpLoginError("tg_password_invalid", "两步验证密码错误") from exc
        if isinstance(exc, errors.FloodWaitError):
            raise RateLimited(
                f"Telegram 限流，需等待 {exc.seconds} 秒",
                int(exc.seconds or 0),
            ) from exc
        raise  # 不可识别 → 原样上抛，路由层统一兜底

    def _finish_locked(self) -> dict[str, Any]:
        session = self._signer.session()
        if not session:
            self._reset_locked()
            raise MtpLoginError("tg_login_failed", "登录未完成：未取得会话")
        self._reset_locked()
        return {"ok": True, "session": session}


_lock = threading.Lock()
_manager: MtpLoginManager | None = None


def _translate_send_code(exc: Exception) -> str:
    """把发码阶段异常翻译成用户可读的中文（含原始细节便于排查）。"""
    from telethon import errors

    if isinstance(exc, errors.PhoneNumberInvalidError):
        return "手机号格式无效（需含国家码，如 +8613800000000）"
    if isinstance(exc, errors.PhoneNumberBannedError):
        return "该手机号已被 Telegram 封禁"
    if isinstance(exc, errors.ApiIdInvalidError):
        return "api_id / api_hash 无效，请检查设置"
    return f"发送验证码失败：{exc}"


def get_login_manager() -> MtpLoginManager:
    """进程内单例（登录上下文本就只存内存，单例天然正确）。

    工厂签名 ``TelethonSigner(**credentials)``——凭据由每次 ``start``
    从设置传入，单例本身不持有凭据（换号只需改设置，无需重启）。
    """
    global _manager
    with _lock:
        if _manager is None:
            _manager = MtpLoginManager(TelethonSigner)
        return _manager
