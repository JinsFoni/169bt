"""AVdb 数据源客户端 —— LTX 破解板块（第二个 ed2k 来源）。

**为什么需要它**：本站原本只采 169bt 的 4K 版块。LTX 破解是另一套数据，
维护在自建的 AVdb 实例里（TG 监控导出 + 资源包导入），通过它的 HTTP API
读取。两者在界面上是顶栏的两个数据源（4K / LTX）。

**为什么登录而不是只用 API Key**（实测，2026-10）：

- ``GET /api/v1/articles/torrents?keyword=`` 认 API Key，但它是**按番号/
  标题模糊搜**，没有分页、没有日期筛选——做不出「和 4K 一样按日期翻」。
- ``POST /api/v1/articles/search`` 能按 ``website/section`` 分页列出，
  但它只认登录 JWT（``security: OAuth2PasswordBearer``）。用 ``X-API-Key``
  或 ``?api_key=``/``?access_token=`` 一律 401 ``Not authenticated``。

因此这里用「用户名 + 密码换 JWT」。JWT 落在设置里（``ltx.jwt``，加密），
失效（401）时用凭据**自动重登一次**再重试——用户不需要关心令牌寿命。

★ 实测 JWT 的 payload 只有 ``{"sub": "...", "ver": 0}``，**没有 exp**：
  过期与否完全由服务端决定（改密码会换 ``ver`` 使旧令牌作废）。
  所以不能「按 exp 提前续」，只能「用到 401 再续」。

★ 本模块的请求**不走应用的网络代理设置**（与论坛/图床不同）：
  AVdb 跑在同一台飞牛的局域网里，走 192.168.0.102:7890 反而连不上。
  这里统一 ``trust_env=False`` 直连。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from bt169 import config

__all__ = [
    "AvdbError",
    "AvdbNotConfigured",
    "AvdbAuthError",
    "AvdbCreds",
    "AvdbClient",
    "LtxSource",
    "load_creds",
    "normalize_item",
    "movie_id_from_url",
    "code_key",
    "female_actors",
    "movie_actress",
    "JWT_KEY",
    "SYNCED_KEY",
    "cover_proxy_url",
    "image_fetch",
]

log = logging.getLogger(__name__)

#: JWT 的存储键（``ltx.jwt``，密钥字段 → 加密落库）。
JWT_KEY = config.section_key("ltx", "jwt")
#: 上次全量同步时间（ISO 8601，非密钥）。
SYNCED_KEY = config.section_key("ltx", "synced_at")


class AvdbError(RuntimeError):
    """AVdb 请求失败（网络、HTTP 错误、返回体不可解析）。"""


class AvdbNotConfigured(AvdbError):
    """还没填地址/账号（设置页 → LTX 数据源）。"""


class AvdbAuthError(AvdbError):
    """鉴权失败：账号密码错、或 JWT 已作废。"""


@dataclass(frozen=True, slots=True)
class AvdbCreds:
    base_url: str
    api_key: str
    username: str
    password: str

    @property
    def configured(self) -> bool:
        """能不能发起同步：**地址 + 账号密码**齐全即可。

        ★ API Key 不是必需的：它只用于 ``torrents`` 那条模糊搜的备用通道。
          但设置页仍允许填——方便用同一个令牌手工调 AVdb。
        """
        return bool(self.base_url and self.username and self.password)


def load_creds(settings: Any) -> AvdbCreds:
    """从设置里读 AVdb 连接参数，做去空白与默认值归一。"""
    get = settings.get

    def _v(key: str) -> str:
        return (get(config.section_key("ltx", key)) or "").strip()

    return AvdbCreds(
        base_url=(_v("base_url") or config.AVDB_DEFAULT_BASE).rstrip("/"),
        api_key=_v("api_key"),
        username=_v("username"),
        password=_v("password"),
    )


def _first_url(value: Any) -> str | None:
    """``preview_images`` → 第一个可用 URL。

    ★ 实测上游给了**三种**形状（同一个字段！）：

    - 单个 URL：``https://…/a.jpg``
    - **逗号分隔的一串**：``https://…/ipx00770pl.jpg,https://…/ipx00770ps.jpg``
      ← 最坑的一种：整体当成 URL 去请求，图床返回 405，界面就是一张破图
    - JSON 数组（少见）

    AVdb 前端也是按逗号切的（``image-cache`` 的 ``split(",")``），照抄它的
    行为，别再猜。
    """
    if isinstance(value, str):
        for part in value.split(","):
            part = part.strip()
            if part.startswith(("http://", "https://")):
                return part
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            found = _first_url(v)
            if found:
                return found
    return None


def normalize_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """AVdb 资源对象 → 本地 ``ltx_items`` 的一行。

    缺 ``publish_date`` 的返回 ``None``：那天放不进月历（``create_time``
    兜底后仍可能为空），宁可丢弃也不要往归档里塞一条日期为空的记录。

    ★ 字段名映射集中在**这一处**：上游是 ``tid/number/magnet/size_bytes/
      preview_images``，本地是 ``id/number/ed2k/size_bytes/cover_url``。
      散落在各路由里改名，改上游就得满仓库找。
    """
    tid = raw.get("tid", raw.get("id"))
    if tid is None:
        return None
    try:
        tid_int = int(tid)
    except (TypeError, ValueError):
        return None

    created = raw.get("create_time") or ""
    publish = (raw.get("publish_date") or created[:10] or "").strip()
    if len(publish) < 10:
        return None

    size_bytes: int | None
    try:
        size_bytes = int(raw.get("size_bytes") or 0) or None
    except (TypeError, ValueError):
        size_bytes = None

    return {
        "id": tid_int,
        "number": (raw.get("number") or "").strip() or None,
        "title": (raw.get("title") or "").strip(),
        "section": (raw.get("section") or "").strip() or None,
        "publish_date": publish[:10],
        "size_bytes": size_bytes,
        "ed2k": (raw.get("magnet") or "").strip() or None,
        "cover_url": _first_url(raw.get("preview_images")),
        "detail_url": (raw.get("detail_url") or "").strip() or None,
        "rejected": 1 if raw.get("rejected") else 0,
        "created_at": created or None,
    }


# --------------------------------------------------------------------- 图片代理

#: 请求图床代理时的目标宽度。
#:
#: ★ 要 **1200**（灯箱档）而不是 600：``ImageCache.ensure()`` 只下载**一次**，
#:   再用同一份字节缩出所有档位。只要 600，灯箱就只能把 600 放大显示。
AVDB_IMAGE_WIDTH = 1200


def cover_proxy_url(base_url: str, src: str, *, width: int = AVDB_IMAGE_WIDTH) -> str:
    """把图床 URL 改写成 AVdb 的图床代理地址。

    ★ **必须走代理，不能直连图床**。实测（2026-10）``tp.spfcas.com`` 对
      直连返回的是**加密后的字节**（159500 B，``Content-Type:
      binary/octet-stream``，没有 JPEG magic，PIL 报 "cannot identify
      image file"）——这是图床的热链保护。同一 URL 经 AVdb 的
      ``/api/v1/img-proxy`` 拿回来就是一张正常 JPEG（800×536）。

      AVdb 前端也是这么做的：``src-lib-image-cache`` 把所有远端图片包成
      ``<base>/api/v1/img-proxy/?url=…&mode=cover&w=…&q=…&fmt=webp``。
      我们照抄它的参数约定，别自己猜。
    """
    if not src:
        return ""
    base = base_url.rstrip("/")
    return (
        f"{base}/api/v1/img-proxy/?url={quote(src, safe='')}"
        f"&mode=cover&w={int(width)}&q=85&fmt=webp&cover_all=0"
    )


def image_fetch(src_url: str, *, base_url: str) -> bytes:
    """下载一张 LTX 封面（经 AVdb 图床代理）。

    ★ ``trust_env=False``：代理地址指向**局域网里的 AVdb**，不能被环境变量
      里的外网代理劫持（与 AvdbClient 同理）。真正的图床出网由 AVdb 自己
      负责——这也是走它的代理带来的附带好处：本机不需要翻墙也能取图。

    ★ 不带 ``Authorization``：实测该端点无需鉴权即可取图；带上令牌反而要
      为一个头像大小的请求先登录一次。
    """
    url = cover_proxy_url(base_url, src_url)
    try:
        resp = httpx.get(
            url,
            headers={"Accept": "image/*", "User-Agent": config.USER_AGENT},
            timeout=config.AVDB_TIMEOUT,
            follow_redirects=True,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
        raise AvdbError(f"封面下载失败：{exc}") from exc
    if getattr(resp, "status_code", 0) != 200:
        raise AvdbError(f"封面下载失败：HTTP {getattr(resp, 'status_code', 0)}")
    return resp.content


# --------------------------------------------------------------------- 演员
#
# LTX 资源对象里**没有演员字段**（``/api/v1/articles/search`` 只给
# ``number/title/magnet/preview_images/detail_url/…``，``actors`` 只出现在它
# 自己的导入载荷 schema 里）。演员藏在 AVdb 的 **javdb 模块**后面：
#
#   ``detail_url = https://javdb.com/v/822NKW``   ← 末尾就是 javdb movie id
#   ``GET /api/v1/javdb/movies/822NKW``           ← **要带 JWT**（OpenAPI 里写
#                                                    ``security: None``，实测
#                                                    不带令牌 401），返回
#                                                    ``actors[] / release_date /
#                                                    maker_name / tags / series``
#
# ★ 为什么**不能一口气全量补**（实测 2026-10）：未缓存一次 0.6–14 秒
#   （长尾严重，1450 条实测 2–5 秒/条，全量要一小时上下）；而且**不能并发**——6 路并行
#   → 40/40 全 502（javdb 侧限流，用的是用户自己的 javdb 账号），
#   串行 +0.5 秒间隔则全成功。
#   所以补法是：同步完之后由 :class:`bt169.actress.ActressBackfiller`
#   **在后台串行、逐条**补齐并落库（``ltx_items.actress``），新的日期优先
#   ——用户正在看的那几天最先有。灯箱那条若还没轮到，``GET /api/ltx/{id}/
#   actress`` 会立刻插一次队查出来（同样落库），不让用户对着空位等。

#: javdb 详情页 URL → movie id。上游给的是 ``https://javdb.com/v/822NKW``，
#: 镜像域名（``javdb580.com`` 等）路径一样，只认路径尾巴。
_RE_MOVIE_URL = re.compile(r"/v/([0-9A-Za-z]{3,12})/?$")

#: 编号归一：``NOSKN-095`` / ``noskn00095`` → ``NOSKN095``。
#: 只用于**核对**取回的电影是不是同一条，不用于展示。
_RE_CODE_NOISE = re.compile(r"[^0-9A-Za-z]+")

#: javdb 的性别枚举：0 = 女，1 = 男。列表里男优女优混在一起。
GENDER_FEMALE = 0


def movie_id_from_url(url: str | None) -> str | None:
    """从 ``detail_url`` 里抠出 javdb movie id；抠不到返回 ``None``。

    ★ 不做「猜 id」：拿不到就退回按编号搜（:func:`movie_actress`），
      因为瞎猜一个 id 会把**别人的**演员挂到这条资源上。
    """
    if not url:
        return None
    found = _RE_MOVIE_URL.search(url.strip())
    return found.group(1) if found else None


def code_key(number: str | None) -> str:
    """编号归一（大写、去掉所有分隔符），用于跨源比对。"""
    return _RE_CODE_NOISE.sub("", (number or "")).upper()


def female_actors(movie: dict[str, Any]) -> list[str]:
    """javdb 电影对象 → 女优名单（去重、保序、去空白）。"""
    out: list[str] = []
    for actor in movie.get("actors") or []:
        if not isinstance(actor, dict):
            continue
        if actor.get("gender") != GENDER_FEMALE:
            continue                      # 男优（如 今井勇太）不是我们要的
        name = (actor.get("name") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _avdb_get(
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    http: Any = None,
) -> dict[str, Any]:
    """AVdb 的 GET + 信封解包（``{"code":0,"data":…}``）。

    ``http`` 只用于测试注入（实现 ``get(url, params=…, timeout=…, headers=…)``）。

    ★ ``trust_env=False``：与其它 AVdb 请求一致，局域网地址不能被环境变量
      里的外网代理劫持。

    ★ **必须带 JWT**：``/api/v1/javdb/*`` 在 OpenAPI 里写着 ``security: None``，
      实测却不带令牌一律 **401** ``{"detail":"API Key 或 Token 鉴权失败"}``
      （AVdb 有全局鉴权中间件，schema 没反映出来）。2026-10-28 实测。
    """
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Accept": "application/json", "User-Agent": config.USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if http is not None:
            resp = http.get(url, params=params, timeout=config.AVDB_TIMEOUT,
                            headers=headers)
        else:
            resp = httpx.get(
                url,
                params=params,
                timeout=config.AVDB_TIMEOUT,
                headers=headers,
                follow_redirects=True,
                trust_env=False,
            )
    except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
        raise AvdbError(f"AVdb 请求失败：{exc}") from exc

    status = getattr(resp, "status_code", 0)
    text = getattr(resp, "text", "") or ""
    if status == 401:
        raise AvdbAuthError("AVdb 鉴权失败（账号密码或令牌无效）")
    if status != 200:
        # ★ 502 是这里的常客：javdb 详情走 AVdb 自己的账号，并发/限流时
        #   上游直接 502。原样报出来，方便一眼看出是「上游忙」而不是坏数据。
        raise AvdbError(f"AVdb 返回 HTTP {status}：{text[:200]}")

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AvdbError(f"AVdb 响应不是 JSON：{text[:200]}") from exc
    if not isinstance(payload, dict):
        raise AvdbError("AVdb 响应格式异常（不是对象）")
    if payload.get("code") not in (0, None):
        raise AvdbError(
            f"AVdb 返回错误：{payload.get('message') or payload.get('code')}"
        )
    return payload


def _search_movie_id(
    base_url: str, number: str, *, token: str | None = None, http: Any = None
) -> str | None:
    """按编号搜 javdb，只认**编号完全一致**的结果。

    ★ 不能拿第一条：``DPMI-020`` 的搜索结果里混着 ``DPMU-020``。搜错一条的
      代价是把别人的女优名字挂到这条资源上——宁可没有。
    """
    if not number:
        return None
    payload = _avdb_get(
        base_url,
        "/api/v1/javdb/search",
        params={"q": number, "limit": 10},
        token=token,
        http=http,
    )
    data = payload.get("data") or {}
    wanted = code_key(number)
    for movie in data.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        if code_key(movie.get("number")) == wanted and movie.get("id"):
            return str(movie["id"])
    return None


def movie_actress(
    base_url: str,
    *,
    detail_url: str | None = None,
    number: str | None = None,
    token: str | None = None,
    http: Any = None,
) -> tuple[str | None, str | None]:
    """查一条资源的演员，返回 ``(演员串, movie_id)``。

    两条路径：``detail_url`` 里有 id 就直接取详情；没有（本地镜像里约 20%
    的条目）就按编号搜一次，只认编号完全匹配的结果。

    查不到不算错——返回 ``(None, None)``。**上游失败才抛**
    :class:`AvdbError`，让调用方区分「真没有」和「这次没问到」；
    令牌失效抛 :class:`AvdbAuthError`（调用方用凭据重登一次再试）。

    演员串用「、」连接（与 4K 侧「出演者」原文一样可能是多人）。
    """
    movie_id = movie_id_from_url(detail_url)
    if movie_id is None:
        movie_id = _search_movie_id(base_url, number or "", token=token, http=http)
    if movie_id is None:
        return None, None

    payload = _avdb_get(
        base_url, f"/api/v1/javdb/movies/{movie_id}", token=token, http=http
    )
    movie = (payload.get("data") or {}).get("movie") or {}
    if not isinstance(movie, dict) or not movie:
        return None, movie_id

    # ★ 编号校验：AVdb 的 LTX 条目有时候会指向邻近的 javdb 页面。取回来的
    #   电影编号与本地编号不一致时**放弃**，而不是把别人的女优写上。
    if number and movie.get("number") and code_key(movie["number"]) != code_key(number):
        log.info(
            "LTX 演员：javdb %s 编号 %s 与资源 %s 不一致，跳过",
            movie_id, movie.get("number"), number,
        )
        return None, movie_id

    names = female_actors(movie)
    return ("、".join(names) or None), movie_id


# --------------------------------------------------------------------- 客户端


class AvdbClient:
    """AVdb 的 HTTP 薄封装（登录 + 资源检索）。

    ``http`` 可注入（测试替身）：只要实现 ``post(url, **kwargs)``。
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        http: Any = None,
        timeout: float = config.AVDB_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._http = http
        self._owns_http = http is None

    # -- 传输层

    def _client(self) -> Any:
        if self._http is None:
            # ★ trust_env=False：AVdb 在局域网，不能被环境变量里的代理劫持。
            self._http = httpx.Client(
                trust_env=False, timeout=self.timeout, follow_redirects=False
            )
        return self._http

    def close(self) -> None:
        if self._owns_http and self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001 — 关连接失败无从处理
                pass
        self._http = None

    def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._client().post(url, timeout=self.timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
            raise AvdbError(f"AVdb 请求失败：{exc}") from exc

        status = getattr(resp, "status_code", 0)
        text = getattr(resp, "text", "") or ""

        if status == 401:
            raise AvdbAuthError("AVdb 鉴权失败（账号密码或令牌无效）")
        if status != 200:
            raise AvdbError(f"AVdb 返回 HTTP {status}：{text[:200]}")

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise AvdbError(f"AVdb 响应不是 JSON：{text[:200]}") from exc

        if not isinstance(payload, dict):
            raise AvdbError("AVdb 响应格式异常（不是对象）")
        # AVdb 统一信封：{"code":0,"message":"…","data":…}
        if payload.get("code") not in (0, None):
            raise AvdbError(
                f"AVdb 返回错误：{payload.get('message') or payload.get('code')}"
            )
        return payload

    # -- 业务接口

    def login(self, username: str, password: str) -> str:
        """用户名密码换 JWT。**表单编码**，不是 JSON（AVdb 的约定）。"""
        if not username or not password:
            raise AvdbNotConfigured(
                "未填写 AVdb 用户名/密码（设置 → LTX 数据源）"
            )
        payload = self._post(
            "/api/v1/users/login",
            data={"username": username, "password": password},
            headers={"Accept": "application/json"},
        )
        data = payload.get("data") or {}
        token = (data.get("access_token") or "").strip()
        if not token:
            raise AvdbAuthError("AVdb 登录未返回 access_token")
        if data.get("requires_2fa"):
            # ★ 账号开了两步验证时登录只到第一步，拿不到可用令牌。
            #   这里明确报错而不是存一个空/半截令牌——否则后续所有请求
            #   都会以「Token 无效」失败，用户看不出根因。
            raise AvdbAuthError(
                "该 AVdb 账号已开启两步验证，本项目只支持单步登录"
            )
        return token

    def search(
        self,
        token: str,
        *,
        page: int = 1,
        page_size: int = config.AVDB_PAGE_SIZE,
        website: str = config.AVDB_LTX_WEBSITE,
        section: str = config.AVDB_LTX_SECTION,
    ) -> dict[str, Any]:
        """按站点/版块分页列出资源。返回 ``data``（含 items/total/hasMore）。"""
        payload = self._post(
            "/api/v1/articles/search",
            json={
                "page": page,
                "page_size": page_size,
                "website": website,
                "section": section,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AvdbError("AVdb 检索响应缺少 data")
        return data


# --------------------------------------------------------------------- 用例层


class LtxSource:
    """LTX 数据源：JWT 缓存 + 翻页拉全量。

    ★ 每次调用都**重读设置**（地址/账号可能刚被改过），但 JWT 复用，
      只有 401 才重登——登录是额外一次请求，没必要每页都换令牌。
    """

    def __init__(self, settings: Any, *, http: Any = None) -> None:
        self._settings = settings
        self._http = http

    # -- 内部

    def _client(self, creds: AvdbCreds) -> AvdbClient:
        return AvdbClient(
            creds.base_url, api_key=creds.api_key, http=self._http
        )

    def _token(self, creds: AvdbCreds, client: AvdbClient, *, force: bool) -> str:
        """取 JWT：库里有就用，没有（或 ``force``）就登录并落库。"""
        if not force:
            cached = (self._settings.get(JWT_KEY) or "").strip()
            if cached:
                return cached
        token = client.login(creds.username, creds.password)
        self._settings.put(JWT_KEY, token)
        log.info("AVdb：已登录（用户 %s）", creds.username)
        return token

    # -- 对外

    def token(self, *, force: bool = False) -> str:
        """拿一个可用 JWT（同一个缓存在 ``ltx.jwt`` 里）。

        ``force=True`` = 丢掉缓存重新登录——只给「401 后重试一次」用。
        演员查询等**不走 ``fetch_all``** 的路径靠它拿令牌。
        """
        creds = load_creds(self._settings)
        if not creds.configured:
            raise AvdbNotConfigured(
                "未配置 LTX 数据源：请到设置 → LTX 数据源填写 AVdb 地址与账号"
            )
        client = self._client(creds)
        try:
            return self._token(creds, client, force=force)
        finally:
            client.close()

    def configured(self) -> bool:
        return load_creds(self._settings).configured

    def lookup_actress(
        self, *, detail_url: str | None = None, number: str | None = None
    ) -> tuple[str | None, str | None]:
        """查一条资源的演员，返回 ``(演员串, movie_id)``。

        这是**演员相关的唯一入口**：背面要地址、要 JWT、401 时要重登一次，
        这些细节接口层和回填程序都不该各写一遍。两条调用路径：

        - ``GET /api/ltx/{id}/actress``（灯箱里那条还没回填到时，立刻查一次）；
        - :class:`bt169.actress.ActressBackfiller`（同步后逐条补齐）。

        Raises:
            AvdbNotConfigured: 没填地址/账号。
            AvdbError: 上游失败（含重登后仍 401 的情况）。
        """
        creds = load_creds(self._settings)
        if not creds.configured:
            raise AvdbNotConfigured(
                "未配置 LTX 数据源：请到设置 → LTX 数据源填写 AVdb 地址与账号"
            )
        token = self.token()
        try:
            return movie_actress(
                creds.base_url, detail_url=detail_url, number=number, token=token
            )
        except AvdbAuthError:
            # ★ 与 ``fetch_all`` 同一个自救策略：令牌被作废（改密码 → ver
            #   变了）时用凭据重登**一次**再试，只重试一次。
            log.info("LTX 演员：JWT 失效，重新登录后重试")
            token = self.token(force=True)
            return movie_actress(
                creds.base_url, detail_url=detail_url, number=number, token=token
            )

    def synced_at(self) -> str | None:
        return self._settings.get(SYNCED_KEY)

    def mark_synced(self, when: str) -> None:
        self._settings.put(SYNCED_KEY, when)

    def fetch_all(self) -> list[dict[str, Any]]:
        """把 LTX 板块**全部**资源拉下来（翻页直到 ``hasMore`` 为假）。

        Returns:
            已 ``normalize_item`` 的字典列表（丢弃日期缺失的行）。

        Raises:
            AvdbNotConfigured: 设置里没有地址/账号。
            AvdbAuthError: 账号密码错、或两步验证。
            AvdbError: 网络/HTTP/格式问题。
        """
        creds = load_creds(self._settings)
        if not creds.configured:
            raise AvdbNotConfigured(
                "未配置 LTX 数据源：请到设置 → LTX 数据源填写 AVdb 地址与账号"
            )

        client = self._client(creds)
        try:
            token = self._token(creds, client, force=False)
            try:
                rows = self._collect(client, token)
            except AvdbAuthError:
                # ★ 令牌被作废（改密码 → ver 变了）时的唯一自救路径：
                #   重登一次再重试，只重试一次，避免把登录玩成暴力破解。
                log.info("AVdb：JWT 失效，重新登录后重试")
                token = self._token(creds, client, force=True)
                rows = self._collect(client, token)
        finally:
            client.close()

        return [r for r in rows if r is not None]

    def _collect(
        self, client: AvdbClient, token: str
    ) -> list[dict[str, Any] | None]:
        out: list[dict[str, Any] | None] = []
        for page in range(1, config.AVDB_MAX_PAGES + 1):
            data = client.search(token, page=page)
            items = data.get("items") or []
            if not items:
                break
            out.extend(normalize_item(it) for it in items if isinstance(it, dict))
            if not data.get("hasMore"):
                break
        return out
