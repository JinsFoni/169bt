"""感谢解锁（C-4）：把 ``pending`` 帖变成 ``done``。

**实测事实**（`/tmp/169bt-recon/FINDINGS.md`）：

- ``POST plugin.php?id=thanksplugin:thanks&action=thanks``
  body：``tid`` / ``formhash`` / ``saying=`` / ``thanksubmit=true``
- 首次返回含 ed2k 的页面；重复返回「已感謝過了」（**幂等**）
- ``formhash`` 从详情页取，与登录页的 formhash **不是**同一个值
"""

from __future__ import annotations

import re

from bt169.source.forum import ForumClient, LoginRequired
from bt169.source.parse import (
    extract_ed2k,
    extract_formhash,
    is_login_required,
    is_thanks_required,
)

__all__ = ["ThanksClient", "ThanksResult", "ThanksError"]

#: 已感谢过的文案（幂等路径）。
_THANKED_MARKERS = ("已感謝過了", "已感谢过了", "已經感謝過", "您已經感謝過")

#: 感谢成功的文案。
_THANKS_OK_MARKERS = ("感謝作者", "感谢作者", "感謝您的支持", "感谢您的支持")

#: 帖子不存在 / 无权访问。
_GONE_MARKERS = ("指定的主题不存在", "本帖要求阅读权限", "您无权进行当前操作")


class ThanksError(RuntimeError):
    """感谢流程失败。

    注意：**重复感谢不算失败**——实测它是幂等的，且页面同样能看到
    ed2k，因此走的是 :class:`ThanksResult` 的成功路径（``already=True``），
    不抛异常。
    """


class ThanksResult:
    """一次感谢尝试的结果。

    ``html`` 是**解锁后**的详情页（若本次流程抓过）。调用方拿它继续解析，
    可以省掉一次 2–5 秒的限速请求——在整轮采集里这是每帖省一次。
    """

    __slots__ = ("tid", "ok", "already", "ed2k", "message", "html")

    def __init__(
        self,
        tid: int,
        *,
        ok: bool,
        already: bool = False,
        ed2k: str | None = None,
        message: str = "",
        html: str | None = None,
    ) -> None:
        self.tid = tid
        self.ok = ok
        self.already = already
        self.ed2k = ed2k
        self.message = message
        self.html = html

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"ThanksResult(tid={self.tid}, ok={self.ok}, "
            f"already={self.already}, ed2k={bool(self.ed2k)})"
        )


class ThanksClient:
    """对需要「感谢」的帖子解锁 ed2k。

    用法::

        tc = ThanksClient(client=fc)
        result = tc.thank(tid)
        if result.ed2k: ...
    """

    def __init__(self, *, client: ForumClient) -> None:
        self._c = client

    def thank(self, tid: int, *, html: str | None = None) -> ThanksResult:
        """感谢一帖并取回 ed2k。

        Args:
            html: 已抓到的详情页 HTML。采集流程刚抓过同一页时传入，
                可省掉一次 2–5 秒的限速请求。

        Raises:
            LoginRequired: 会话失效（调用方应中止任务并触发重登录）。
            ThanksError: 页面结构异常或帖子已不存在。
        """
        if html is None:
            html = self._c.raw_thread_html(tid)

        if is_login_required(html):
            raise LoginRequired(f"帖子 {tid} 需要登录才能感谢")

        # 已能直接看到 ed2k（此前已感谢过，或本就不需感谢）→ 无需再发请求
        direct = extract_ed2k(html)
        if direct:
            return ThanksResult(
                tid, ok=True, ed2k=direct, message="已有 ed2k", html=html
            )

        if not is_thanks_required(html):
            return ThanksResult(
                tid, ok=False, message="页面既无 ed2k 也无感谢按钮"
            )

        formhash = extract_formhash(html)
        if not formhash:
            raise ThanksError(f"帖子 {tid} 详情页缺少 formhash")

        body = self._c.post(
            "plugin.php?id=thanksplugin:thanks&action=thanks",
            data={
                "tid": str(tid),
                "formhash": formhash,
                "saying": "",
                "thanksubmit": "true",
            },
            headers={"Referer": f"{self._c.base}/forum.php?mod=viewthread&tid={tid}"},
        ).text

        already = any(m in body for m in _THANKED_MARKERS)
        if any(m in body for m in _GONE_MARKERS):
            raise ThanksError(f"帖子 {tid} 不存在或无权访问")

        # ★ 无论首次还是重复，都以「重新 GET 详情页能否拿到 ed2k」为准。
        #   实测重复感谢返回「已感謝過了」，但页面里同样能看到 ed2k。
        after = self._c.raw_thread_html(tid)
        if is_login_required(after):
            raise LoginRequired(f"帖子 {tid} 在感谢后要求登录")

        ed2k = extract_ed2k(after)
        if ed2k:
            return ThanksResult(
                tid,
                ok=True,
                already=already,
                ed2k=ed2k,
                message="已感謝過了" if already else "感谢成功",
                html=after,
            )

        if already:
            # 已感谢过但仍无 ed2k：可能是回复可见而非感谢可见
            return ThanksResult(
                tid, ok=False, already=True,
                message="已感谢但未出现 ed2k", html=after,
            )

        ok_marker = any(m in body for m in _THANKS_OK_MARKERS)
        return ThanksResult(
            tid,
            ok=False,
            message="感谢已提交但未出现 ed2k"
            if ok_marker
            else _snippet(body),
            html=after,
        )


def _snippet(body: str, limit: int = 120) -> str:
    """从响应里取一段可读文本，供错误信息使用。"""
    text = re.sub(r"<[^>]*>", " ", body)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "感谢请求返回空响应"
