"""Telegram ed2k 转发（需求 T-1~T-8 / 架构 §5.5）。

设计要点（均来自实测与架构决策）：

- **用 ``sendMessage`` 而非 ``forwardMessage``**：Telegram 的
  ``forwardMessage`` 要求源消息已存在于某个会话，而我们手里只有一段
  ed2k 文本（需求里的「转发」是中文口语意义的「发过去」）。
- **ed2k 独占一行**：Bot 侧（aria2 / qBittorrent 插件）通常按行提取链接，
  混在文字里会解析失败。
- **幂等**：发送前查 ``tg_sent_at``，成功后写入。语义是「已提交」而非
  「已下载」。
- **限流**：Telegram 群组约 20 条/分钟；429 响应带 ``retry_after``，必须遵守。
- **不阻塞**（T-8）：TG 不可达只影响转发本身，绝不影响采集与浏览。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from bt169.models import Post

__all__ = [
    "TelegramClient",
    "TelegramError",
    "TelegramNotConfigured",
    "AlreadySent",
    "RateLimited",
    "build_message",
    "forward_post",
    "forward_many",
    "API_BASE",
    "MIN_INTERVAL_SECONDS",
]

log = logging.getLogger(__name__)

#: Bot API 基址。测试通过注入 ``http`` 替身来避免真实网络。
#: ``BT169_TG_API_BASE`` 可覆盖——api.telegram.org 在部分网络下不可达，
#: 用户需要指向自建反代。
API_BASE = os.environ.get("BT169_TG_API_BASE") or "https://api.telegram.org"

#: 批量转发时每条之间的最小间隔。
#: ★ 群组限制约 20 条/分钟 → 3 秒是安全值（留足余量给别的客户端）。
#: 实测依据：架构 §5.5.3。
MIN_INTERVAL_SECONDS = 3.0

#: 单条消息长度上限（Telegram 限制 4096，留余量）。
MAX_MESSAGE_LEN = 4000


class TelegramError(RuntimeError):
    """转发失败。"""


class TelegramNotConfigured(TelegramError):
    """尚未配置 Bot Token / Chat ID（需求 T-4）。"""


class AlreadySent(TelegramError):
    """该帖已转发过（需求 T-7 幂等）。"""


class RateLimited(TelegramError):
    """被 Telegram 限流（429）。``retry_after`` 为服务端建议的等待秒数。"""

    def __init__(self, message: str, retry_after: int = 0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HTTPLike(Protocol):
    """只需 ``post``，便于测试注入。"""

    def post(self, url: str, **kwargs: Any) -> Any: ...


# --------------------------------------------------------------------- 消息


def build_message(post: Post) -> str:
    """构造要发送的文本。

    格式（ed2k **必须独占一行**）::

        【ABC-101】某人 2026-09-17 7GB
        ed2k://|file|...

    无 ed2k 时抛 :class:`TelegramError`——需求 T-5 明确禁止转发未解锁帖。
    """
    if not post.ed2k:
        raise TelegramError(
            f"帖子 {post.tid} 没有 ed2k 链接，无法转发（尚未解锁）"
        )

    head_bits = [f"【{post.code or post.tid}】"]
    if post.actress:
        head_bits.append(post.actress)
    if post.release_date:
        head_bits.append(post.release_date)
    if post.size:
        head_bits.append(post.size)

    msg = " ".join(head_bits) + "\n" + post.ed2k
    return msg[:MAX_MESSAGE_LEN]


# --------------------------------------------------------------------- 客户端


@dataclass
class TelegramClient:
    """Telegram Bot API 客户端。

    ``http`` 可注入（测试用替身；生产用 ``httpx.Client``）。
    """

    token: str
    chat_id: str
    http: HTTPLike
    #: ★ 不能用 ``base: str = API_BASE``——数据类的默认值是**类定义时**
    #: 求值的，改模块级 ``API_BASE`` 不会带动它（同 "import 时算出的
    #: 派生常量" 那类坑）。用 None 占位、在 ``__post_init__`` 里取，
    #: 既让测试能改，也让用户能指向自建反代（api.telegram.org 在某些
    #: 网络下不可达）。
    base: str | None = None
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.base is None:
            self.base = API_BASE
        if not self.token or not self.chat_id:
            raise TelegramNotConfigured(
                "尚未配置 Telegram Bot Token 或 Chat ID"
            )
        self.token = self.token.strip()
        self.chat_id = str(self.chat_id).strip()
        self.base = self.base.rstrip("/")

    def send_message(self, text: str) -> int:
        """发送一条消息，返回 ``message_id``。

        Raises:
            RateLimited: 429；``retry_after`` 是服务端建议的等待秒数。
            TelegramError: 其他失败（含网络异常）。
        """
        url = f"{self.base}/bot{self.token}/sendMessage"
        try:
            resp = self.http.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    # ★ 不解析 Markdown/HTML：ed2k 里的 ``|`` 与 ``_`` 会被
                    #   当成标记语法，导致 Telegram 报 "can't parse entities"。
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
            raise TelegramError(f"Telegram 请求失败：{exc}") from exc

        status = getattr(resp, "status_code", 0)
        body = getattr(resp, "text", "")

        if status == 429:
            retry_after = 0
            try:
                payload = json.loads(body)
                retry_after = int(
                    payload.get("parameters", {}).get("retry_after", 0)
                )
            except Exception:  # noqa: BLE001 — 解析失败就用默认值
                pass
            raise RateLimited(
                f"Telegram 限流，需等待 {retry_after} 秒", retry_after
            )

        if status != 200:
            raise TelegramError(f"Telegram 返回 HTTP {status}：{body[:200]}")

        try:
            payload = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise TelegramError(f"Telegram 响应不是 JSON：{body[:200]}") from exc

        if not payload.get("ok"):
            desc = payload.get("description", "未知错误")
            raise TelegramError(f"Telegram 拒绝：{desc}")

        return int(payload.get("result", {}).get("message_id", 0))

    def test(self) -> None:
        """发一条测试消息，验证凭据可用（设置面板「测试」按钮）。"""
        self.send_message("169bt 测试消息：Telegram 配置可用。")


# --------------------------------------------------------------------- 转发


def forward_post(
    post: Post,
    *,
    client: TelegramClient,
    posts_repo: Any,
) -> int:
    """转发单个帖子，成功后写入 ``tg_sent_at``。

    Raises:
        AlreadySent: 已转发过（需求 T-7 幂等——重复点「下载」不重复发送）。
        TelegramError: 无 ed2k 或发送失败。
    """
    if post.tg_sent_at is not None:
        raise AlreadySent(f"帖子 {post.tid} 已于 {post.tg_sent_at} 转发过")

    text = build_message(post)
    message_id = client.send_message(text)
    posts_repo.mark_tg_sent(post.tid, _now())
    log.info("帖子 %s 已转发到 Telegram（message_id=%s）", post.tid, message_id)
    return message_id


def forward_many(
    posts: list[Post],
    *,
    client: TelegramClient,
    posts_repo: Any,
    interval: float = MIN_INTERVAL_SECONDS,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """批量转发（需求 T-2）。

    ★ **串行 + 间隔 ≥ ``interval`` 秒**：Telegram 群组约 20 条/分钟，
    并发发送必然触发 429，反而更慢。

    ★ **单条失败不中断整批**：一条发不出去不该让后面所有帖子都失败。
    失败原因记进 ``errors``，让前端能逐条重试（需求 T-6）。

    Returns:
        ``{"sent": int, "skipped": int, "failed": int, "errors": [...]}``
    """
    sent = skipped = failed = 0
    errors: list[dict[str, Any]] = []

    for i, post in enumerate(posts):
        if i > 0:
            sleep(interval)
        try:
            forward_post(post, client=client, posts_repo=posts_repo)
            sent += 1
        except AlreadySent:
            skipped += 1
        except RateLimited as exc:
            # ★ 被限流就停手：继续发只会越撞越久。把剩余帖子如实报告为失败，
            #   让用户稍后重试——比硬撑下去触发更长的封禁好。
            failed += 1
            errors.append({"tid": post.tid, "error": str(exc)})
            remaining = posts[i + 1:]
            for p in remaining:
                failed += 1
                errors.append({
                    "tid": p.tid,
                    "error": f"因限流跳过（前一条需等待 {exc.retry_after} 秒）",
                })
            break
        except TelegramError as exc:
            failed += 1
            errors.append({"tid": post.tid, "error": str(exc)})

    return {"sent": sent, "skipped": skipped, "failed": failed, "errors": errors}


def _now() -> str:
    from bt169.repo.posts import now_iso

    return now_iso()
