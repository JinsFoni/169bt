"""论坛登录：验证码预校验 + 额度保护 + 熔断。

**实测事实**（`/tmp/169bt-recon/FINDINGS.md`，2026-09-18）：

- 登录页有**两套 hash**，必须区分：
  ``loginhash``（来自 ``<form id="loginform_XXXXX">``，用于登录 POST 的 URL）
  与 **seccode idhash**（来自 ``<span id="seccode_YYYYYY">``，用于取图与 check）。
  实测二者不同，混用会静默失败。
- 单图 check 上限 **恰好 3 次（总次数）**，第 4 次必 ``invalid``——**即使提交正确码**。
  成功的 check 也计数；换图重置计数。
- 提交**错误验证码不消耗登录额度**（服务端直接返回「验证码填写错误」）。
  这正是「先 check 再提交」策略的依据。
- 登录额度：**5 次 / 900 秒**（Discuz ``logincheck()``，按 IP）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bt169.source.captcha import CaptchaSolver, Solver
from bt169.source.forum import FetchError, ForumClient
from bt169.source.session import (
    RELOGIN_FAILED,
    RELOGIN_OK,
    RELOGIN_RETRYING,
    SessionStore,
)

__all__ = ["LoginClient", "LoginResult", "LoginError", "QuotaExhausted"]

#: ``<span id="seccode_XXXXXX">`` 里的 idhash。
_SECCODE_ID_RE = re.compile(r'<span id="seccode_([A-Za-z0-9]+)"')

#: ``<form id="loginform_XXXXX">`` 里的 loginhash。
_LOGINHASH_RE = re.compile(r'id="loginform_([A-Za-z0-9]+)"')

_FORMHASH_RE = re.compile(r'name="formhash" value="([^"]+)"')

#: CDATA 内容。
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

#: 服务端返回的额度耗尽文案。
_QUOTA_MARKERS = ("尝试次数过多", "请 15 分钟后再试", "登录失败次数过多")

#: 验证码错误文案（**不消耗额度**）。
_CAPTCHA_ERROR_MARKERS = ("验证码填写错误", "验证码错误", "请填写验证码")


class LoginError(RuntimeError):
    """登录失败。"""


class QuotaExhausted(LoginError):
    """登录额度耗尽，必须等待。"""


@dataclass(frozen=True, slots=True)
class LoginResult:
    """一次登录尝试的结果。"""

    ok: bool
    message: str
    attempts_left: int | None = None
    quota_exhausted: bool = False
    captcha_images: int = 0


@dataclass(frozen=True, slots=True)
class _LoginForm:
    """登录页上一次性有效的参数。"""

    formhash: str
    loginhash: str
    idhash: str
    referer: str


class LoginClient:
    """论坛登录。

    用法::

        lc = LoginClient(client=fc, store=store, solvers=[PythonSolver()])
        result = lc.login("user", "pass")
    """

    def __init__(
        self,
        *,
        client: ForumClient,
        store: SessionStore,
        solvers: list[Solver],
        base: str | None = None,
    ) -> None:
        self._c = client
        self._store = store
        self._solvers = solvers
        self._base = (base or self._c.base).rstrip("/")

    # ------------------------------------------------------------ 页面解析

    def _load_form(self) -> _LoginForm:
        """抓登录页并取出所有一次性参数。"""
        html = self._c.get("member.php?mod=logging&action=login",
                            count_rate=False).text

        m = _FORMHASH_RE.search(html)
        if not m:
            raise LoginError("登录页缺少 formhash——页面结构可能已变动")
        formhash = m.group(1)

        m = _LOGINHASH_RE.search(html)
        if not m:
            raise LoginError("登录页缺少 loginhash")
        loginhash = m.group(1)

        # ★ seccode idhash 与 loginhash 是**不同的值**。缺失说明当前
        #   IP 被免验证码（Discuz 对可信 IP 会关闭验证码）。
        m = _SECCODE_ID_RE.search(html)
        idhash = m.group(1) if m else ""

        return _LoginForm(
            formhash=formhash,
            loginhash=loginhash,
            idhash=idhash,
            referer=f"{self._base}/member.php?mod=logging&action=login",
        )

    # ------------------------------------------------------------ 验证码

    def _make_solver(self, form: _LoginForm) -> CaptchaSolver:
        """构造验证码求解器（取图 + 预校验都绑定到当前会话）。"""
        import time

        def fetch_image() -> bytes:
            return self._c.get_bytes(
                f"misc.php?mod=seccode&update={int(time.time() * 1000)}"
                f"&idhash={form.idhash}",
                headers={"Accept": "image/*", "Referer": form.referer},
            )

        def check(code: str) -> bool:
            from bt169.source.captcha import parse_seccode_check

            body = self._c.get(
                f"misc.php?mod=seccode&action=check&inajax=1"
                f"&modid=member::logging&idhash={form.idhash}"
                f"&secverify={code}",
                headers={"Referer": form.referer,
                         "X-Requested-With": "XMLHttpRequest"},
                count_rate=False,
            ).text
            return parse_seccode_check(body)

        return CaptchaSolver(
            solvers=self._solvers, fetch_image=fetch_image, check=check
        )

    # ------------------------------------------------------------ 登录

    def login(self, username: str, password: str) -> LoginResult:
        """执行一次完整登录。

        ★ **先预校验验证码再提交**：实测提交错误验证码不消耗登录额度，
        因此可以把「猜验证码」的成本完全挡在额度之外。
        """
        if not username or not password:
            raise LoginError("用户名与密码不能为空")

        self._store.set_relogin_state(RELOGIN_RETRYING)
        form = self._load_form()

        code = ""
        images = 0
        if form.idhash:
            solved = self._make_solver(form).solve()
            if solved is None:
                self._store.record_attempt(attempts_left=None,
                                           state=RELOGIN_FAILED)
                return LoginResult(
                    ok=False,
                    message="验证码识别失败（已换满 6 张图）",
                    captcha_images=6,
                )
            code = solved.code
            images = solved.images_used

        result = self._submit(form, username, password, code)
        if result.ok:
            self._store.save_cookies(self._c.cookies, username=username)
            self._store.record_attempt(attempts_left=None, state=RELOGIN_OK)
        else:
            self._store.record_attempt(
                attempts_left=result.attempts_left, state=RELOGIN_FAILED
            )
        return LoginResult(
            ok=result.ok,
            message=result.message,
            attempts_left=result.attempts_left,
            quota_exhausted=result.quota_exhausted,
            captcha_images=images,
        )

    def _submit(
        self, form: _LoginForm, username: str, password: str, code: str
    ) -> LoginResult:
        """提交登录表单并解读响应。"""
        body = self._c.post(
            f"member.php?mod=logging&action=login&loginsubmit=yes"
            f"&loginhash={form.loginhash}&inajax=1",
            data={
                "formhash": form.formhash,
                "referer": f"{self._base}/",
                "loginfield": "username",
                "username": username,
                "password": password,
                "questionid": "0",
                "answer": "",
                "seccodehash": form.idhash,
                "seccodemodid": "member::logging",
                "seccodeverify": code,
                "cookietime": "2592000",
                "loginsubmit": "true",
            },
            headers={"Referer": form.referer,
                     "X-Requested-With": "XMLHttpRequest"},
        ).text

        return self._interpret(body)

    @staticmethod
    def _interpret(body: str) -> LoginResult:
        """把登录响应翻译成结果。

        Discuz 的 inajax 响应把提示文本放在 CDATA 里。
        """
        m = _CDATA_RE.search(body)
        text = (m.group(1) if m else body).strip()
        lowered = text.lower()

        # 成功：Discuz 返回「欢迎您回来」或直接是跳转脚本
        if "欢迎您回来" in text or "succeed" in lowered or "现在将转入登录前页面" in text:
            return LoginResult(ok=True, message=text or "登录成功")

        # 额度耗尽
        if any(marker in text for marker in _QUOTA_MARKERS):
            return LoginResult(
                ok=False, message=text, attempts_left=0, quota_exhausted=True
            )

        # 验证码错误：不消耗额度
        if any(marker in text for marker in _CAPTCHA_ERROR_MARKERS):
            return LoginResult(ok=False, message=text, attempts_left=None)

        left = _parse_attempts_left(text)
        return LoginResult(ok=False, message=text or "登录失败", attempts_left=left)


#: 「还可以尝试 4 次」/「您还可以尝试 4 次登录」
_ATTEMPTS_RE = re.compile(r"还?可以尝试\s*(\d+)\s*次")


def _parse_attempts_left(text: str) -> int | None:
    """从失败文案里解析剩余额度。

    实测文案形如「登录失败，您还可以尝试 4 次」。
    """
    m = _ATTEMPTS_RE.search(text)
    return int(m.group(1)) if m else None
