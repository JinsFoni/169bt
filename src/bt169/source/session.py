"""会话状态与 Cookie 持久化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bt169.db import Database
from bt169.repo.posts import now_iso

__all__ = [
    "ForumSession",
    "SessionStore",
    "COOKIE_LIFETIME_DAYS",
    "MAX_LOGIN_ATTEMPTS",
    "SAFETY_MARGIN",
    "LOGIN_WINDOW_SECONDS",
    "MIN_SECONDS_BETWEEN_LOGIN",
    "can_attempt_login",
    "login_block_reason",
    "RenewResult",
    "SessionRenewer",
]

#: 实测 Cookie 有效期（`cookietime=2592000` 秒 = 30 天）。
COOKIE_LIFETIME_DAYS = 30

#: 提前多少天开始主动续期（ARCHITECTURE.md §5.2.2）。
RENEW_BEFORE_DAYS = 5

#: 登录额度（ARCHITECTURE.md §5.2）。Discuz 默认按 IP 计 5 次 / 900 秒。
MAX_LOGIN_ATTEMPTS = 5

#: 永远留 1 次不打——最后 1 次是「用户手动救命」用的。
SAFETY_MARGIN = 1

#: 服务端窗口：超过这个时长未再失败，计数自动重置（实测推断）。
LOGIN_WINDOW_SECONDS = 900

#: 两次登录提交的最小间隔（ARCHITECTURE.md §5.2 规则 3）。
#: 目的是避免「连着撞」触发风控——额度按 IP 计，撞狠了可能连正常
#: 访问一起被限。
MIN_SECONDS_BETWEEN_LOGIN = 600


def login_block_reason(session: ForumSession | None) -> str | None:
    """不能登录时的**机器可读**原因码；可以登录时返回 ``None``。

    ★ 与 :func:`can_attempt_login` 分开，是因为「给用户看的话」和
    「给代码判断的值」是两种东西。前者会随文案调整而变，后者必须是
    稳定的标识符——续期器要按它决定记录哪个状态。

    原因码：``"quota"`` / ``"too_soon"``。
    """
    if session is None:
        return None

    left = session.login_attempts_left
    if left is not None:
        # ★ 窗口重置：服务端 900 秒未再失败就把计数清零。
        #   不重置的话「上次失败过」会永久堵住登录，而实际上额度早回来了。
        last = session.last_login_attempt
        if last:
            try:
                elapsed = (
                    datetime.now().astimezone() - datetime.fromisoformat(last)
                ).total_seconds()
                if elapsed > LOGIN_WINDOW_SECONDS:
                    left = None          # 视为已重置
            except ValueError:
                pass
        if left is not None and left <= SAFETY_MARGIN:
            return "quota"

    last = session.last_login_attempt
    if last:
        try:
            elapsed = (
                datetime.now().astimezone() - datetime.fromisoformat(last)
            ).total_seconds()
        except ValueError:
            return None
        if elapsed < MIN_SECONDS_BETWEEN_LOGIN:
            return "too_soon"

    return None


def can_attempt_login(session: ForumSession | None) -> tuple[bool, str]:
    """是否可以再提交一次登录。返回 ``(允许, 给用户看的原因)``。

    ★ 这是**额度保护的唯一入口**。ARCHITECTURE.md §5.2 定下的规则：

    - 规则 2：剩余次数 ≤ ``SAFETY_MARGIN`` 时拒绝（留 1 次救命）
    - 规则 3：距上次提交不足 ``MIN_SECONDS_BETWEEN_LOGIN`` 时拒绝

    之所以做成**纯函数**而不是埋在 :meth:`LoginClient.login` 里，
    是为了让「为什么不让登录」可测、可读——额度是稀缺资源，
    拒绝的理由必须能讲清楚。

    判定逻辑在 :func:`login_block_reason`，本函数只负责把它翻译成人话。
    """
    reason = login_block_reason(session)
    if reason is None:
        return True, ""

    assert session is not None

    if reason == "quota":
        return False, (
            f"登录额度仅剩 {session.login_attempts_left} 次"
            f"（安全下限 {SAFETY_MARGIN}），拒绝提交以免耗尽；"
            f"请等 {LOGIN_WINDOW_SECONDS // 60} 分钟窗口重置"
        )

    elapsed = (
        datetime.now().astimezone()
        - datetime.fromisoformat(session.last_login_attempt or "")
    ).total_seconds()
    wait = int(MIN_SECONDS_BETWEEN_LOGIN - elapsed)
    return False, (
        f"距上次登录提交仅 {int(elapsed)} 秒，为避免风控需再等 {wait} 秒"
    )


#: 重登录状态。
RELOGIN_OK = "ok"
RELOGIN_RETRYING = "retrying"
RELOGIN_FAILED = "failed"
RELOGIN_BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ForumSession:
    """论坛会话快照。"""

    cookies: dict[str, str]
    username: str | None
    obtained_at: str
    expires_at: str | None
    valid: bool
    login_attempts_left: int | None
    last_login_attempt: str | None
    last_relogin_at: str | None
    relogin_state: str | None

    @property
    def days_left(self) -> int | None:
        """距预计失效的天数；无 expires_at 返回 None。"""
        if not self.expires_at:
            return None
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return None
        delta = exp - datetime.now().astimezone()
        return max(0, delta.days)

    def needs_renewal(self) -> bool:
        """是否已进入主动续期窗口。"""
        days = self.days_left
        return days is not None and days <= RENEW_BEFORE_DAYS


class SessionStore:
    """``forum_session`` 表的读写。

    表结构是单行（``id=1``）——个人自用，永远只有一个论坛会话。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def load(self) -> ForumSession | None:
        row = self._db.read().execute(
            "SELECT * FROM forum_session WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        try:
            cookies = json.loads(row["cookies"] or "{}")
        except json.JSONDecodeError:
            cookies = {}
        return ForumSession(
            cookies=cookies,
            username=row["username"],
            obtained_at=row["obtained_at"],
            expires_at=row["expires_at"],
            valid=bool(row["valid"]),
            login_attempts_left=row["login_attempts_left"],
            last_login_attempt=row["last_login_attempt"],
            last_relogin_at=row["last_relogin_at"],
            relogin_state=row["relogin_state"],
        )

    def save_cookies(
        self, cookies: dict[str, str], *, username: str | None = None
    ) -> ForumSession:
        """保存新登录得到的 Cookie，并计算预计失效时间。"""
        now = datetime.now().astimezone()
        expires = (now + timedelta(days=COOKIE_LIFETIME_DAYS)).isoformat(
            timespec="seconds"
        )
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO forum_session(id, cookies, username, obtained_at,"
                " expires_at, valid, last_relogin_at, relogin_state)"
                " VALUES(1,?,?,?,?,1,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " cookies=excluded.cookies, username=excluded.username,"
                " obtained_at=excluded.obtained_at, expires_at=excluded.expires_at,"
                " valid=1, last_relogin_at=excluded.last_relogin_at,"
                " relogin_state=excluded.relogin_state",
                (
                    json.dumps(cookies, ensure_ascii=False),
                    username,
                    now_iso(),
                    expires,
                    now_iso(),
                    RELOGIN_OK,
                ),
            )
        loaded = self.load()
        assert loaded is not None
        return loaded

    def _ensure_row(self, conn) -> None:  # type: ignore[no-untyped-def]
        """保证单行存在。

        ★ 首次登录就失败时表里还没有任何行，纯 ``UPDATE`` 会**影响 0 行**、
        静默丢掉额度信息（"还可以尝试 3 次"）。
        """
        conn.execute(
            "INSERT INTO forum_session(id, cookies, obtained_at, valid)"
            " VALUES(1, '{}', ?, 0) ON CONFLICT(id) DO NOTHING",
            (now_iso(),),
        )

    def mark_invalid(self) -> None:
        with self._db.write() as conn:
            self._ensure_row(conn)
            conn.execute("UPDATE forum_session SET valid=0 WHERE id=1")

    def record_attempt(
        self, *, attempts_left: int | None, state: str | None = None
    ) -> None:
        """记录一次登录提交的结果。

        Args:
            attempts_left: 服务器返回的剩余次数（仅失败时才有）。
            state: :data:`RELOGIN_OK` / :data:`RELOGIN_FAILED` / ...
        """
        with self._db.write() as conn:
            self._ensure_row(conn)
            conn.execute(
                "UPDATE forum_session SET last_login_attempt=?,"
                " login_attempts_left=COALESCE(?, login_attempts_left),"
                " relogin_state=COALESCE(?, relogin_state) WHERE id=1",
                (now_iso(), attempts_left, state),
            )

    def set_relogin_state(self, state: str) -> None:
        with self._db.write() as conn:
            self._ensure_row(conn)
            conn.execute(
                "UPDATE forum_session SET relogin_state=? WHERE id=1", (state,)
            )


# ---------------------------------------------------------------- 主动续期


@dataclass(frozen=True, slots=True)
class RenewResult:
    """一次续期尝试的结果。"""

    attempted: bool
    ok: bool
    reason: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass
class SessionRenewer:
    """会话主动续期（W-16 / ARCHITECTURE.md §5.2.2）。

    ★ **登录是稀缺资源**：实测 Discuz 按 IP 限 5 次 / 900 秒自动重置。
    因此这里的每一步都偏向「不动手」：

    - 只在**进入续期窗口**（≤ :data:`RENEW_BEFORE_DAYS` 天）或会话**已失效**时动手
    - 额度 ≤1 直接拒绝（:data:`RELOGIN_BLOCKED`）
    - 距上次提交 < :data:`MIN_SECONDS_BETWEEN_LOGIN` 直接拒绝
    - 没配凭据直接拒绝（会白烧一次额度）

    ★ **绝不抛异常**：调用方可能是采集流程或后台定时器。续期失败不该
    让采集失败，也不该静默杀死后台线程。

    用法::

        renewer = SessionRenewer(store=store, login_client=lc,
                                 credentials=lambda: (user, pw))
        result = renewer.maybe_renew()
    """

    store: Any
    login_client: Any
    credentials: Any          # Callable[[], tuple[str, str]]

    def maybe_renew(self) -> RenewResult:
        session = self.store.load()
        if session is None:
            return RenewResult(False, True, "no_session")

        if not self._should_renew(session):
            return RenewResult(False, True, "not_needed")

        ok, why = can_attempt_login(session)
        if not ok:
            # ★ 额度/频率不足：**不尝试**，如实记下状态。
            #   「额度不够还去试」正是把额度耗光的做法。
            self._set_state(RELOGIN_BLOCKED)
            # 机器可读的原因码（"quota" / "too_soon"），why 是给人看的文案
            return RenewResult(False, False, login_block_reason(session) or "",
                               why)

        try:
            username, password = self.credentials()
        except Exception:  # noqa: BLE001 — 解密失败等
            return RenewResult(False, True, "no_credentials")

        if not username or not password:
            return RenewResult(False, True, "no_credentials")

        try:
            result = self.login_client.login(username, password)
        except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
            self._set_state(RELOGIN_FAILED)
            return RenewResult(True, False, "error", str(exc))

        if not getattr(result, "ok", False):
            self._set_state(RELOGIN_FAILED)
            msg = getattr(result, "message", "") or "登录失败"
            return RenewResult(True, False, "login_failed", msg)

        self._set_state(RELOGIN_OK)
        return RenewResult(True, True, "renewed")

    # ------------------------------------------------------------ 内部

    def _should_renew(self, session: ForumSession) -> bool:
        """是否该续期。

        - 会话已失效 → 是（与剩余天数无关）
        - 拿不到 ``expires_at`` → **否**：不知道还剩多久就不该赌，
          而且 :meth:`ForumSession.needs_renewal` 也会返回 False
        - 否则看是否进入续期窗口
        """
        if not session.valid:
            return True
        return session.needs_renewal()

    def _set_state(self, state: str) -> None:
        try:
            self.store.set_relogin_state(state)
        except Exception:  # noqa: BLE001 — 记状态失败不该影响主流程
            pass
