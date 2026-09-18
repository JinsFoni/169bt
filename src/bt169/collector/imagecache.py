"""图片本地化（W-15）：下载 → 缩放 → WebP → 本地存储。

**为什么在采集时做，而不是请求时**：

- 采集是**串行限速**的（每帖 2–5 s），转码 ~100 ms 完全可忽略
- 请求时做会让首次浏览变慢，并发转码还会吃满 CPU
- 预生成后 ``StaticFiles`` 直接分发，零开销

**实测依据**（源图 ``2184×1542 / 922 KB``，本机实测）：

```
600px  → 600×424  / 67.1 KB  （省 92.7%）
1200px → 1200×847 / 189.6 KB （省 79.5%）
```

**存储布局**（文件名 = ``sha1(源 URL)[:16]``，天然去重）：

```
data/images/
└── ab/
    ├── ab3f9c1d2e4f5a6b-600.webp
    └── ab3f9c1d2e4f5a6b-1200.webp
```

**删除联动**：硬删除帖子时要删图，但必须先做**引用计数**——
同一张图可能被多个帖子引用。计数依据是**源 URL**：因为
``key = sha1(源 URL)``，同一 key 必然来自同一 URL，所以按 URL 计数是精确的。
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

from bt169 import config

__all__ = ["ImageCache", "ImageError", "SIZES", "CARD_WIDTH", "LIGHTBOX_WIDTH"]

log = logging.getLogger(__name__)

#: 卡片缩略图宽度。
CARD_WIDTH = 600
#: 灯箱大图宽度。
LIGHTBOX_WIDTH = 1200
#: 预生成的宽度档位。
SIZES = (CARD_WIDTH, LIGHTBOX_WIDTH)

#: WebP 质量（实测 q80 是体积/观感的拐点）。
QUALITY = 80

#: 单张图最大允许体积（防图床返回异常大文件把磁盘写满）。
MAX_BYTES = 20 * 1024 * 1024


class ImageError(RuntimeError):
    """图片下载或转码失败。"""


@dataclass(frozen=True, slots=True)
class CachedImage:
    """一张已本地化的图。"""

    url: str
    key: str
    #: 宽度 → 相对 URL（如 ``/img/ab/ab3f-600.webp``）
    variants: dict[int, str]
    bytes_in: int
    bytes_out: int


def _default_fetch(url: str) -> bytes:
    """默认下载实现（图片在独立图床，不受论坛限速约束）。"""
    import httpx

    resp = httpx.get(
        url,
        headers={"User-Agent": config.USER_AGENT, "Accept": "image/*"},
        timeout=30.0,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


class ImageCache:
    """图片本地化缓存。

    用法::

        cache = ImageCache()
        cached = cache.ensure(url)          # 下载 + 转码 + 落盘
        cached.variants[600]                # /img/ab/ab3f-600.webp
    """

    def __init__(
        self,
        root: Path | str = config.IMAGE_DIR,
        *,
        fetch: Callable[[str], bytes] | None = None,
        sizes: tuple[int, ...] = SIZES,
    ) -> None:
        self.root = Path(root)
        self._fetch = fetch or _default_fetch
        self._sizes = tuple(sorted(sizes))

    # ------------------------------------------------------------ 路径计算

    @staticmethod
    def key_for(url: str) -> str:
        """源 URL → 内容键（16 位十六进制）。

        同 URL 必然同 key，因此**天然去重**：多个帖子引用同一张图只存一份。
        """
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def path_for(self, key: str, width: int) -> Path:
        """内容键 + 宽度 → 本地绝对路径。"""
        return self.root / key[:2] / f"{key}-{width}.webp"

    def url_for(self, key: str, width: int) -> str:
        """内容键 + 宽度 → 前端可用的相对 URL。"""
        return f"/img/{key[:2]}/{key}-{width}.webp"

    def has(self, url: str) -> bool:
        """该源 URL 的全部档位是否都已存在。"""
        key = self.key_for(url)
        return all(self.path_for(key, w).exists() for w in self._sizes)

    # ------------------------------------------------------------ 主流程

    def ensure(self, url: str, *, force: bool = False) -> CachedImage:
        """确保 ``url`` 已本地化，返回各档位的相对 URL。

        已存在时**不发起任何网络请求**。

        Raises:
            ImageError: 下载失败、响应不是图片、或体积超限。
        """
        key = self.key_for(url)
        if not force and self.has(url):
            return CachedImage(
                url=url, key=key,
                variants={w: self.url_for(key, w) for w in self._sizes},
                bytes_in=0,
                bytes_out=sum(self.path_for(key, w).stat().st_size
                              for w in self._sizes),
            )

        try:
            raw = self._fetch(url)
        except Exception as exc:  # noqa: BLE001 —— 图床的任何故障都不该中断采集
            raise ImageError(f"下载失败：{exc}") from exc

        if not raw:
            raise ImageError("下载到空响应")
        if len(raw) > MAX_BYTES:
            raise ImageError(
                f"图片过大：{len(raw) / 1024 / 1024:.1f} MB "
                f"（上限 {MAX_BYTES / 1024 / 1024:.0f} MB）"
            )

        try:
            # ★ 必须校验：图床出错时返回的是 HTML 错误页，若直接存成
            #   .webp 就会产生一个永远显示不出来的「成功」缓存。
            with Image.open(io.BytesIO(raw)) as probe:
                probe.verify()
            img = Image.open(io.BytesIO(raw))
            img.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ImageError(f"响应不是有效图片：{exc}") from exc

        variants: dict[int, str] = {}
        out_bytes = 0
        for width in self._sizes:
            path = self.path_for(key, width)
            data = self._render(img, width)
            self._atomic_write(path, data)
            out_bytes += len(data)
            variants[width] = self.url_for(key, width)

        return CachedImage(
            url=url, key=key, variants=variants,
            bytes_in=len(raw), bytes_out=out_bytes,
        )

    def ensure_post(self, post) -> dict[str, CachedImage]:  # type: ignore[no-untyped-def]
        """把一条帖子的封面图与详情图本地化。

        返回 ``{"cover": CachedImage, "detail": CachedImage}``，缺图或
        失败的**不出现**在结果里——调用方据此决定是否更新 DTO。

        本方法**不抛异常**：图片失败不该让整帖采集失败（帖子其余字段
        已经拿到了，比图重要得多）。
        """
        out: dict[str, CachedImage] = {}
        for field in ("cover", "detail"):
            url = getattr(post, f"{field}_img", None)
            if not url:
                continue
            try:
                out[field] = self.ensure(url)
            except ImageError as exc:
                log.warning("帖子 %s 的 %s 图本地化失败：%s",
                            getattr(post, "tid", "?"), field, exc)
        return out

    # ------------------------------------------------------------ 转码

    def _render(self, img: Image.Image, width: int) -> bytes:
        """缩放并编码为 WebP。

        只缩不放：源图比目标还窄时保持原尺寸，避免糊化。
        """
        w, h = img.size
        if w > width:
            new_size = (width, max(1, round(h * width / w)))
            resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
        else:
            resized = img.convert("RGB")

        buf = io.BytesIO()
        resized.save(buf, "WEBP", quality=QUALITY, method=6)
        return buf.getvalue()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """原子写入：先写临时文件再 rename。

        避免进程被杀时留下**半张图**——那种文件会被 ``has()`` 判定为
        已缓存，于是永远显示不全。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    # ------------------------------------------------------------ 删除联动

    def delete(self, url: str) -> int:
        """删除某 URL 的全部档位，返回删除的文件数。"""
        key = self.key_for(url)
        removed = 0
        for width in self._sizes:
            path = self.path_for(key, width)
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    def total_bytes(self) -> int:
        """已占用的磁盘空间。"""
        return sum(p.stat().st_size for p in self.root.rglob("*.webp")
                   if p.is_file())

    def orphans(self, known_keys: set[str]) -> list[Path]:
        """找出没有任何帖子引用的图片文件（供 ``169bt doctor`` 清理）。"""
        out = []
        if not self.root.exists():
            return out
        for path in self.root.rglob("*.webp"):
            if path.is_file() and path.stem.rsplit("-", 1)[0] not in known_keys:
                out.append(path)
        return out
