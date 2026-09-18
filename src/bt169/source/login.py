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
    RELOGIN_BLOCKED,
    RELOGIN_FAILED,
    RELOGIN_OK,
    RELOGIN_RETRYING,
    SessionStore,
    can_attempt_login,
)

__all__ = ["LoginClient", "LoginResult", "LoginError", "QuotaExhausted"]

#: ``<span id="seccode_XXXXXX">`` 里的 idhash。
_SECCODE_ID_RE = re.compile(r'<span id="seccode_([A-Za-z0-9]+)"')

#: ``<form id="loginform_XXXXX">`` 里的 loginhash。
_LOGINHASH_RE = re.compile(r'id="loginform_([A-Za-z0-9]+)"')

_FORMHASH_RE = re.compile(r'name="formhash" value="([^"]+)"')

#: CDATA 内容。
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

#: Discuz 的 inajax 响应把**提示文案和一段回调脚本一起**塞进 CDATA。
#:
#: 响应有**两种形状**，清洗必须同时成立（否则会误判登录结果）：
#:
#: 1. **失败**——文案裸在 CDATA 里，后面跟回调脚本::
#:
#:        登录失败，您还可以尝试 4 次<script type="text/javascript"
#:        reload="1">errorhandle_('登录失败，您还可以尝试 4 次', {});</script>
#:
#: 2. **成功**——**整段响应都是脚本**，成功文案嵌在 JS 字符串里::
#:
#:        <script>…$('succeedlocation').innerHTML =
#:        '欢迎您回来，新手上路 ymxh，现在将转入登录前页面';</script>
#:
#: ★ 踩过的坑：只做「剥掉所有 ``<script>``」会把形状 2 的文案一起剥掉
#: → 明明登录成功（已拿到 ``SlDj_2132_auth``）却报「登录失败」。
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

#: JS 里写进 DOM 的字符串，形如 ``$('succeedlocation').innerHTML = '文案'``。
#: 抓的是**赋值右侧的字面量**——那是 Discuz 放成功文案的地方。
#:
#: ★ 用 ``.*?`` 而非 ``.+?``：响应里有 ``innerHTML = ''`` 这种**空串**
#: 赋值，``.+?`` 匹配不上它会一路吃掉引号跨到下一个字符串里
#: （实测会抓出 ``"';}if(typeof succeedhandle_=="`` 这种垃圾）。
#: 允许空串命中、再由调用方跳过，才是对的。
_JS_STRING_RE = re.compile(
    r"(?:innerHTML|innerText|textContent)\s*=\s*"
    r"(['\"])(?P<msg>.*?)\1",
    re.S,
)


def clean_message(text: str) -> str:
    """把响应剥成人类可读的提示。

    两种响应形状都要处理（见 ``_SCRIPT_RE`` 上方说明）：

    - 先取**裸文本**（剥掉 script 与标签）
    - 裸文本为空时，再去 JS 字符串里找（成功响应只有后者）

    ★ 顺序不能反：失败响应的文案在裸文本里，而回调脚本里有一份**相同**
    的副本。若先去 JS 里找，会把 ``errorhandle_('…')`` 的参数当成文案
    ——虽然内容一样，但语义上该展示的是页面上那段。
    """
    plain = _TAG_RE.sub("", _SCRIPT_RE.sub("", text)).strip()
    if plain:
        return plain

    # 裸文本为空 → 成功响应那种「全是脚本」的形状：从 JS 字符串里取
    for m in _JS_STRING_RE.finditer(text):
        candidate = m.group("msg").strip()
        # 跳过空串（``$('succeedmessage').innerHTML = ''``）
        if candidate:
            return candidate
    return ""

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

        ★ **额度保护**（ARCHITECTURE.md §5.2 规则 2/3）：提交前先查
        :func:`can_attempt_login`。额度是稀缺资源（实测 5 次 / 900 秒，
        按 IP 计），撞光了就得干等——所以宁可拒绝也不赌。
        """
        if not username or not password:
            raise LoginError("用户名与密码不能为空")

        allowed, reason = can_attempt_login(self._store.load())
        if not allowed:
            self._store.set_relogin_state(RELOGIN_BLOCKED)
            return LoginResult(ok=False, message=reason)

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

        # ★ 提交前快照 cookie：要区分「本次响应**新下发**的 auth cookie」与
        #   「客户端本来就带着的旧 auth cookie」。后者在重登场景下必然存在
        #   （旧会话过期了但 cookie 还在），若只看「有没有」会把失败当成功。
        before = dict(self._c.cookies)

        result = self._submit(form, username, password, code)

        # ★ 以 **auth cookie 为准**判定成败，不以文案为准。
        #   实测踩过的坑：清洗规则把成功文案（嵌在 JS 字符串里）剥掉后，
        #   登录**其实成功了**（拿到了 SlDj_2132_auth）却报「登录失败」。
        #   文案只是展示层，cookie 才是服务端接受凭据的证据。
        has_session = _fresh_auth_cookie(before, self._c.cookies)
        if not result.ok and has_session:
            # 文案没说是成功（或清洗规则漏了），但服务端确实下发了新会话。
            result = LoginResult(
                ok=True,
                message=result.message or "登录成功",
                attempts_left=result.attempts_left,
            )
        elif result.ok and not has_session:
            # ★ 反向：文案说成功，却没有新下发的 auth cookie → **不能算成功**。
            #   没有会话 cookie，后续每个请求都会被当匿名处理，「成功」是假的。
            #   宁可在这里响亮地失败，也不要把一个用不了的会话存进库。
            result = LoginResult(
                ok=False,
                message="登录响应缺少会话凭据（服务端未下发认证 Cookie）",
                attempts_left=result.attempts_left,
            )

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
        # ★ 先剥脚本/标签再判断：否则 ``<script>`` 里的内容会污染
        #   文案（实测用户看到的就是一整坨 JS）。
        text = clean_message(m.group(1) if m else body)
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

#: Discuz 的认证 cookie 名形如 ``SlDj_2132_auth``（前缀由站点配置决定）。
#: ★ 这是**权威的成功信号**：它是服务端接受凭据后下发的，也是后续
#: 所有请求能通过鉴权的前提。文案只是展示层，会随语言包/版本变。
_AUTH_COOKIE_RE = re.compile(r"(?:^|_)(?:auth|saltkey)$", re.I)


def _fresh_auth_cookie(
    before: dict[str, str] | None, after: dict[str, str] | None
) -> bool:
    """``after`` 里是否有**本次新下发**的 auth cookie。

    ★ 为什么要比 ``before``：重登时客户端已经带着旧的（过期的）
    ``_auth`` cookie。只看「after 里有没有」的话，一次失败的登录提交
    也会因为旧 cookie 还在而被判成功——接着上层拿这个会话去请求，
    全部 401，比直接报失败难查得多。

    判定「新」的条件：after 有该 cookie，且（before 没有，或值变了）。
    """
    if not after:
        return False
    for name, value in after.items():
        if not value or not _AUTH_COOKIE_RE.search(name):
            continue
        if before is None or before.get(name) != value:
            return True
    return False


def has_auth_cookie(cookies: dict[str, str] | None) -> bool:
    """cookies 里是否含 Discuz 的认证 cookie。

    Discuz 登录成功后下发 ``{前缀}_auth``（以及 ``_saltkey``）。
    前缀由站点配置决定（实测本站为 ``SlDj_2132``），所以只比对后缀。

    ★ 有它 = 服务端确实接受了凭据；没有它，文案再像成功也不能信。
    """
    if not cookies:
        return False
    return any(
        _AUTH_COOKIE_RE.search(name) and bool(value)
        for name, value in cookies.items()
    )


def _parse_attempts_left(text: str) -> int | None:
    """从失败文案里解析剩余额度。

    实测文案形如「登录失败，您还可以尝试 4 次」。
    """
    m = _ATTEMPTS_RE.search(text)
    return int(m.group(1)) if m else None
