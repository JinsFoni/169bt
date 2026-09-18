"""论坛 HTTP 客户端：限速、会话、列表与详情抓取。

**并发度恒为 1**，且每次请求之间有 2–5 秒随机间隔（C-7）。
这不是性能取舍，是账号安全要求。

本模块只负责「拿到 HTML」，解析交给 :mod:`bt169.source.parse`。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from bt169 import config
from bt169.source.parse import (
    ListRow,
    ThreadDetail,
    is_login_required,
    parse_thread_detail,
    parse_thread_list,
)

__all__ = ["ForumClient", "RateLimiter", "FetchError", "LoginRequired"]


class FetchError(RuntimeError):
    """网络或 HTTP 层错误。"""


class LoginRequired(RuntimeError):
    """会话失效，需要重新登录。"""


class RateLimiter:
    """串行限速器。

    只保证**最小间隔**；调用方自己保证串行（本客户端天然串行）。
    """

    def __init__(
        self,
        delay_range: tuple[float, float] = config.FETCH_DELAY_RANGE,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._lo, self._hi = delay_range
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._last: float | None = None

    def wait(self) -> float:
        """睡够间隔，返回实际睡了多少秒。"""
        if self._last is None:
            self._last = time.monotonic()
            return 0.0
        delay = self._rng.uniform(self._lo, self._hi)
        self._sleep(delay)
        self._last = time.monotonic()
        return delay


@dataclass(slots=True)
class _Page:
    """一次抓取的结果。"""

    url: str
    text: str
    status: int


class ForumClient:
    """论坛客户端。

    用法::

        with ForumClient(cookies=...) as fc:
            rows = fc.list_page(fid="192", page=1)
    """

    def __init__(
        self,
        *,
        cookies: dict[str, str] | None = None,
        base: str = config.FORUM_BASE,
        limiter: RateLimiter | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        user_agent: str = config.USER_AGENT,
    ) -> None:
        self.base = base.rstrip("/")
        self._limiter = limiter or RateLimiter()
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None
        # ★ 必须**强制**带浏览器 UA：实测论坛对默认 ``python-httpx/x.y.z``
        #   会返回异常页。注意不能用 ``setdefault``——httpx 会自己先填上
        #   库默认 UA，导致我们被静默忽略。
        self._client.headers["User-Agent"] = user_agent
        self._client.headers["Accept-Language"] = "zh-CN,zh;q=0.9,ja;q=0.8"
        if cookies:
            for k, v in cookies.items():
                self._client.cookies.set(k, v)

    # -------------------------------------------------------------- 生命周期

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ForumClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._client.cookies)

    # -------------------------------------------------------------- 底层请求

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        count_rate: bool = True,
    ) -> _Page:
        """GET 一个相对路径。

        Args:
            path: 形如 ``forum.php?mod=forumdisplay&fid=192``。
            headers: 额外请求头（验证码图需要 ``Accept: image/*``）。
            count_rate: 是否计入限速（验证码相关请求不计——它们是登录流程的
                一部分，与浏览抓取无关）。
        """
        if count_rate:
            self._limiter.wait()

        url = path if path.startswith("http") else f"{self.base}/{path}"
        try:
            resp = self._client.get(url, headers=headers or {})
        except httpx.HTTPError as exc:
            raise FetchError(f"请求失败：{url}：{exc}") from exc

        # 302 跳登录页 = 会话失效
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if "mod=logging" in loc:
                raise LoginRequired(f"会话失效，被重定向到登录页：{loc}")
            raise FetchError(f"意外的重定向：{loc}")

        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code}：{url}")

        return _Page(url=url, text=resp.text, status=resp.status_code)

    def get_bytes(self, path: str, *, headers: dict[str, str] | None = None) -> bytes:
        """取二进制内容（验证码图）。**不计入浏览限速**。"""
        url = path if path.startswith("http") else f"{self.base}/{path}"
        try:
            resp = self._client.get(url, headers=headers or {})
        except httpx.HTTPError as exc:
            raise FetchError(f"请求失败：{url}：{exc}") from exc
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code}：{url}")
        return resp.content

    def post(
        self,
        path: str,
        *,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
        count_rate: bool = False,
    ) -> _Page:
        """POST 表单。默认不计入浏览限速（登录/感谢有各自的节奏）。"""
        if count_rate:
            self._limiter.wait()
        url = path if path.startswith("http") else f"{self.base}/{path}"
        try:
            resp = self._client.post(url, data=data, headers=headers or {})
        except httpx.HTTPError as exc:
            raise FetchError(f"请求失败：{url}：{exc}") from exc
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code}：{url}")
        return _Page(url=url, text=resp.text, status=resp.status_code)

    # -------------------------------------------------------------- 业务方法

    def list_page(self, *, fid: str = config.DEFAULT_FID, page: int = 1) -> list[ListRow]:
        """抓取版块列表页的某页。

        Args:
            fid: 版块 ID。
            page: 页码，从 1 开始。

        Returns:
            该页的行（论坛按发帖时间降序）。
        """
        html = self.get(
            f"forum.php?mod=forumdisplay&fid={fid}&page={page}"
        ).text
        return parse_thread_list(html)

    def fetch_thread(self, tid: int) -> ThreadDetail:
        """抓取帖子详情页。

        Raises:
            LoginRequired: 页面呈现登录墙。
        """
        html = self.get(f"forum.php?mod=viewthread&tid={tid}").text
        if is_login_required(html):
            raise LoginRequired(f"帖子 {tid} 需要登录才能查看")
        return parse_thread_detail(html, tid)

    def raw_thread_html(self, tid: int) -> str:
        """取详情页原始 HTML（感谢流程需要二次解析）。"""
        return self.get(f"forum.php?mod=viewthread&tid={tid}").text
