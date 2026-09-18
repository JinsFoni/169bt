"""Emby 入库标记（P7 / 需求 E-1~E-7 / 架构 §5.4）。

**核心架构约束：浏览路径绝不调用 Emby。**

    EmbySyncer（每 15 分钟或手动）
        │  拉媒体库 → 提取番号 → 匹配 → 写回 posts.emby_status
        ▼
    浏览请求 → 读 posts.emby_status（纯本地，永不阻塞）   ← E-7 降级

Emby 挂掉时，浏览照常工作，只是卡片上少了「已入库」标记。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from bt169.source.parse import extract_codes

__all__ = [
    "EmbyClient",
    "EmbyError",
    "EmbyNotConfigured",
    "EmbySyncer",
    "SyncResult",
    "index_items",
    "EMBY_API_BASE",
]

log = logging.getLogger(__name__)

#: Emby 服务地址。测试通过注入 ``http`` 替身避免真实网络。
#: ``BT169_EMBY_API_BASE`` 可覆盖（自建反代 / 内网穿透场景）。
EMBY_API_BASE = os.environ.get("BT169_EMBY_API_BASE") or ""

#: 单页条目数。Emby 默认较小，显式放大以减少往返。
PAGE_SIZE = 500

#: 翻页上限（防御失控循环：TotalRecordCount 异常时不至于死循环）。
MAX_PAGES = 100


class EmbyError(RuntimeError):
    """Emby 调用失败。"""


class EmbyNotConfigured(EmbyError):
    """尚未配置 Emby 地址或 API Key。"""


class HTTPLike(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


def default_http(timeout: float = 15.0) -> Any:
    """生产用的 httpx 客户端。

    ★ **``trust_env=False`` 是必需的**，不是可选项。

    httpx 默认读 ``http_proxy`` / ``https_proxy`` 环境变量。Emby 几乎总是
    跑在 **局域网**（``192.168.1.10:8096``），把这些地址丢给一个互联网
    代理（Clash / V2Ray 之类）只会得到 502。

    实测（本机开着 ``http_proxy=http://127.0.0.1:7890``）：

        trust_env=True  → HTTP 502          ← 代理代不通局域网
        trust_env=False → ConnectError      ← 真实的「连不上」

    502 比「连接被拒」难查得多：它看起来像 Emby 挂了，实际是代理在乱接。
    """
    import httpx

    return httpx.Client(timeout=timeout, trust_env=False)


# ---------------------------------------------------------------- 索引构造


def index_items(rows: list[dict[str, Any]]) -> dict[str, str]:
    """把 Emby 条目列表转成 ``{番号: item_id}`` 索引。

    ★ 同时看 ``Name`` 与 ``Path``：Emby 的 ``Name`` 常是清洗过的标题，
    真正的番号往往在 ``Path`` 的文件名里（``s169bbs.com@START-624_[4K].mkv``）。
    只看 Name 会漏掉大量条目。

    ★ 用 ``extract_codes`` 做**双向提取 + 精确比对**，而不是拿本地番号去
    ``in`` 判断——后者会让 ``START-62`` 命中 ``START-624``（E-5 假阳性）。
    """
    idx: dict[str, str] = {}
    for row in rows:
        item_id = row.get("Id")
        if not item_id:
            # 没有 Id 的条目无法用于匹配。塞 None 进索引会让后续
            # 「已入库」判定为真却拿不出 item_id。
            continue
        for field_name in ("Name", "Path"):
            text = row.get(field_name)
            if not text:
                continue
            for code in extract_codes(str(text)):
                # 同一个番号出现多次（Name 与 Path 都有）时保留先到的：
                # 没有理由让后到的覆盖，且先到的是 Name（更可能是正片）。
                idx.setdefault(code, str(item_id))
    return idx


# ---------------------------------------------------------------- 客户端


@dataclass
class EmbyClient:
    """Emby REST 客户端（只读媒体库列表）。"""

    url: str
    api_key: str
    http: HTTPLike
    library_id: str | None = None
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if not self.url or not self.api_key:
            raise EmbyNotConfigured("尚未配置 Emby 地址或 API Key")
        self.url = self.url.strip().rstrip("/")
        self.api_key = self.api_key.strip()
        self.library_id = (self.library_id or "").strip() or None
    def list_items(self) -> list[dict[str, Any]]:
        """拉取媒体库全部条目（自动翻页）。

        Raises:
            EmbyError: 网络失败、非 200、或响应不是合法 JSON。
        """
        out: list[dict[str, Any]] = []
        start = 0

        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "Path",
                "Limit": PAGE_SIZE,
                "StartIndex": start,
            }
            # E-4：只匹配设置页指定的媒体库
            if self.library_id:
                params["ParentId"] = self.library_id

            payload = self._get("/Items", params)
            rows = payload.get("Items") or []
            out.extend(rows)

            total = payload.get("TotalRecordCount")
            start += len(rows)

            # 没有条目、拿不到总数、或已取满 → 结束
            if not rows or not isinstance(total, int) or start >= total:
                break

        return out

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._get_raw(path, params)
        if not isinstance(payload, dict):
            raise EmbyError("Emby 响应结构异常（不是对象）")
        return payload

    def _get_raw(self, path: str, params: dict[str, Any]) -> Any:
        """发 GET 并解析 JSON。**不假设**顶层是对象——

        ``/Library/VirtualFolders`` 返回的是**数组**，``/Items`` 返回对象。
        """
        try:
            resp = self.http.get(
                self.url + path,
                params=params,
                # ★ 用 header 而非 query 传密钥：query 会进服务器访问日志，
                #   把 API Key 写成明文。
                headers={"X-Emby-Token": self.api_key,
                         "Accept": "application/json"},
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
            raise EmbyError(f"Emby 请求失败：{exc}") from exc

        status = getattr(resp, "status_code", 0)
        body = getattr(resp, "text", "")

        if status != 200:
            raise EmbyError(f"Emby 返回 HTTP {status}：{body[:200]}")

        try:
            return json.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise EmbyError(f"Emby 响应不是 JSON：{body[:200]}") from exc


# ---------------------------------------------------------------- 同步器


@dataclass
class SyncResult:
    """一次同步的结果统计。"""

    checked: int = 0        # 查询过的本地帖数
    in_library: int = 0     # 判定为已入库
    library_items: int = 0  # Emby 侧条目数
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "in_library": self.in_library,
            "library_items": self.library_items,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class EmbySyncer:
    """把 Emby 媒体库状态同步进本地 DB。

    ★ **绝不抛异常**：E-7 要求 Emby 不可达时不影响浏览。调用方拿到的是
    带 ``error`` 的 :class:`SyncResult`，而不是异常。
    """

    client: EmbyClient
    posts_repo: Any
    clock: Any = field(default=None)  # 注入用；None → 用 repo 的 now_iso

    def sync(self) -> SyncResult:
        """拉媒体库 → 匹配 → 写回。失败时返回带 ``error`` 的结果。"""
        from bt169.repo.posts import now_iso

        checked_at = now_iso()

        try:
            rows = self.client.list_items()
        except EmbyError as exc:
            log.warning("Emby 同步失败（不影响浏览）：%s", exc)
            return SyncResult(error=str(exc))

        idx = index_items(rows)
        result = SyncResult(library_items=len(rows))

        for post in self.posts_repo.all_posts():
            if not post.code:
                continue
            item_id = idx.get(post.code.upper())
            self.posts_repo.set_emby(
                post.tid,
                in_library=item_id is not None,
                item_id=item_id,
                checked_at=checked_at,
            )
            result.checked += 1
            if item_id is not None:
                result.in_library += 1

        log.info(
            "Emby 同步完成：库内 %s 条，检查 %s 帖，命中 %s",
            result.library_items, result.checked, result.in_library,
        )
        return result


# ---------------------------------------------------------------- 定时同步


#: 默认同步间隔（E-6 建议 10–30 分钟）。
SYNC_INTERVAL_SECONDS = 15 * 60


@dataclass
class EmbyScheduler:
    """按间隔重复同步的后台循环（E-6）。

    ★ **绝不抛异常**：Emby 挂了、没配置、网络超时——全部吞掉记日志。
    这是后台线程，异常逃出去只会静默杀死线程（用户从此再也没有标记）。

    ★ 用「算下一次该跑的时刻」而不是 ``sleep(interval)``：
    同步本身可能耗时数十秒，固定 sleep 会让实际间隔变成
    ``interval + 同步耗时``，越漂越远。
    """

    build_syncer: Any            # Callable[[], EmbySyncer | None]
    interval: float = SYNC_INTERVAL_SECONDS
    stop_event: Any = None       # threading.Event；None → 自己建
    clock: Any = None            # time.monotonic；注入用于测试

    def __post_init__(self) -> None:
        import threading
        import time

        if self.stop_event is None:
            self.stop_event = threading.Event()
        if self.clock is None:
            self.clock = time.monotonic
        self._thread: Any = None

    def tick(self) -> SyncResult | None:
        """跑一次同步。未配置或失败都返回 None / 带 error 的结果，不抛。"""
        try:
            syncer = self.build_syncer()
        except EmbyNotConfigured:
            return None          # 没配 → 静默跳过，不是错误
        except Exception as exc:  # noqa: BLE001
            log.warning("Emby 同步器构造失败：%s", exc)
            return SyncResult(error=str(exc))

        if syncer is None:
            return None
        try:
            return syncer.sync()
        except Exception as exc:  # noqa: BLE001 — 最后一道防线
            log.warning("Emby 同步异常：%s", exc)
            return SyncResult(error=str(exc))

    def run(self) -> None:
        """循环直到 ``stop_event`` 被设置。"""
        while not self.stop_event.is_set():
            self.tick()
            deadline = self.clock() + self.interval
            while not self.stop_event.is_set():
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                # 用 wait 而不是 sleep：停止信号能立刻打断等待
                self.stop_event.wait(min(remaining, 5.0))

    def start(self) -> None:
        import threading

        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.run, name="emby-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
