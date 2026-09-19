"""图片本地化（W-15）：下载 → 缩放 → WebP → 本地存储。

**为什么在采集时做，而不是请求时**：

- 采集是**串行限速**的（每帖 2–5 s），转码 ~100 ms 完全可忽略
- 请求时做会让首次浏览变慢，并发转码还会吃满 CPU
- 预生成后 ``StaticFiles`` 直接分发，零开销

**实测依据**（源图 ``2184×1542 / 922.4 KB``，本机实测）：

```
600px  → 600×424  / 67.1 KB  （省 92.7%）
1200px → 1200×847 / 189.6 KB （省 79.5%）
原图   → 2184×1542 / 922.4 KB（★ 原样存源字节，零转码）
```

**三档各有明确用途**（``FIELD_TARGETS`` 决定谁用哪档）：

- **600px** —— 卡片缩略图（有损 WebP，卡片只要 1.8% 的像素）
- **1200px** —— 中间档
- **原图** —— ★ 灯箱看大图。**原样保存下载到的字节**，不转码、不缩放。
  实测灯箱面板 1162 CSS px、图片显示 1129px，600px 那档被放大 1.88×
  （DPR=2 时 3.8×）→ 糊。原图是**缩下来**显示的，任何 DPR 下都不糊。

**为什么原图不转 WebP**：源图本身就是有损 JPEG，再编码只会更差。
实测对比（同一张源图）：

```
原样存 JPEG 字节   922.4 KB   PSNR ∞      （零损失）← 选这个
有损 WebP q80      474.8 KB   PSNR 34.24 dB
无损 WebP         3206.7 KB   PSNR ∞      （大 3.5 倍）
```

省下的 447 KB 不值得引入一次额外有损编码——用户要看的就是原图。

**存储布局**（文件名 = ``sha1(源 URL)[:16]``，天然去重）：

```
data/images/
└── ab/
    ├── ab3f9c1d2e4f5a6b-600.webp
    ├── ab3f9c1d2e4f5a6b-1200.webp
    └── ab3f9c1d2e4f5a6b-orig.jpg   ← 原图：后缀是 ``orig``，扩展名随源格式
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

__all__ = [
    "ImageCache",
    "ImageError",
    "SIZES",
    "CARD_WIDTH",
    "LIGHTBOX_WIDTH",
    "ORIGINAL",
    "suffix_for",
    "matches_width",
]

log = logging.getLogger(__name__)

#: 卡片缩略图宽度。
CARD_WIDTH = 600
#: 中间档宽度。
LIGHTBOX_WIDTH = 1200

#: ★ 原图档的哨兵值。
#:
#: 用 ``0`` 而非某个具体宽度：原图宽度是**运行时才知道**的（取决于源图），
#: 写成常量就会说谎。``0`` 在 ``ensure`` 里表达「不缩放、不转码」。
#: 文件名后缀也因此不用数字，改用 ``-orig``——``-0.webp`` 看不出是什么。
ORIGINAL = 0

#: 源图格式 → 文件扩展名。
#:
#: ★ 原图**原样存字节**，所以扩展名必须随源格式：JPEG 存成 ``.webp``
#: 会让静态服务以错误的 Content-Type 分发（浏览器靠嗅探勉强能显示，
#: 但那是运气，不是设计）。
_EXT_BY_FORMAT: dict[str, str] = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "TIFF": "tiff",
}

#: 预生成的档位。
#:
#: ★ 原图档**默认生成**：它由灯箱消费，漏了就又变成
#: 「要用时没生成」。磁盘代价实测 ~922 KB/张（源图 2184×1542）。
SIZES = (CARD_WIDTH, LIGHTBOX_WIDTH, ORIGINAL)

#: WebP 质量（实测 q80 是体积/观感的拐点）。
QUALITY = 80

#: 单张图最大允许体积（防图床返回异常大文件把磁盘写满）。
MAX_BYTES = 20 * 1024 * 1024


class ImageError(RuntimeError):
    """图片下载或转码失败。"""


def suffix_for(width: int) -> str:
    """档位 → 文件名后缀（``orig`` 或宽度数字）。

    ★ 公开函数而非私有方法：补图时要用它判断「库里记的档位对不对」
    （见 ``Collector._repair_images``）。生成与判断必须同源，
    否则改一处漏一处就会静默跳过。
    """
    return "orig" if width == ORIGINAL else str(width)


def matches_width(local_url: str, width: int) -> bool:
    """本地图路径是否就是 ``width`` 这一档。

    ★ 不能简单地 ``endswith(suffix_for(width))``：

    - 原图档的真实后缀是 ``-orig.jpg`` / ``-orig.png``（随源格式），
      拿 ``"orig"`` 比是 True 但拿完整名比就漏了扩展名
    - 宽度档要带上 ``.webp``，否则 ``-1600.webp`` 会被 ``600`` 误命中

    所以按「``-`` + 档位 + ``.``」匹配：``-600.`` / ``-1200.`` / ``-orig.``。
    """
    return f"-{suffix_for(width)}." in local_url


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

    @staticmethod
    def _suffix(width: int) -> str:
        """档位 → 文件名后缀。原图档是 ``orig``，其余是宽度数字。"""
        return suffix_for(width)

    def path_for(self, key: str, width: int, *, fmt: str | None = None) -> Path:
        """内容键 + 宽度 → 本地绝对路径。

        ``fmt`` 只在原图档用得上（扩展名随源格式）。求原图**已存在**的
        路径请用 :meth:`original_path`——那里是 glob，不靠猜扩展名。
        """
        if width == ORIGINAL:
            ext = _EXT_BY_FORMAT.get((fmt or "JPEG").upper(), "jpg")
            return self.root / key[:2] / f"{key}-orig.{ext}"
        return self.root / key[:2] / f"{key}-{self._suffix(width)}.webp"

    def original_path(self, key: str) -> Path | None:
        """原图档在磁盘上的实际路径（没有则 ``None``）。

        ★ 用 glob 而不是拼扩展名：扩展名取决于源图格式，库里的路径是
        采集时写下的。猜错（比如一律猜 ``.jpg``）会判定成「不存在」→
        每次都重新下载，或者更糟：判定成「不存在」而文件其实在，
        于是一个 key 下堆两份原图。
        """
        matches = sorted((self.root / key[:2]).glob(f"{key}-orig.*"))
        return matches[0] if matches else None

    def url_for(self, key: str, width: int) -> str:
        """内容键 + 宽度 → 前端可用的相对 URL。"""
        if width == ORIGINAL:
            path = self.original_path(key)
            if path is None:      # 兜底：文件不在时给个形状正确的 URL
                return f"/img/{key[:2]}/{key}-orig.jpg"
            return f"/img/{key[:2]}/{path.name}"
        return f"/img/{key[:2]}/{key}-{self._suffix(width)}.webp"

    def has(self, url: str) -> bool:
        """该源 URL 的全部档位是否都已存在。"""
        return len(self._disk_variants(self.key_for(url))) == len(self._sizes)

    def _disk_variants(self, key: str) -> dict[int, str]:
        """磁盘上**实际存在**的档位 → 相对 URL。"""
        out: dict[int, str] = {}
        for width in self._sizes:
            path = (self.original_path(key) if width == ORIGINAL
                    else self.path_for(key, width))
            if path is not None and path.exists():
                out[width] = f"/img/{key[:2]}/{path.name}"
        return out

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
                variants=self._disk_variants(key),
                bytes_in=0,
                bytes_out=sum(
                    p.stat().st_size for p in self._files_for(key)
                ),
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
            if width == ORIGINAL:
                # ★ 原图 = **原样存下载到的字节**。不转码、不缩放——
                #   源图本身已是有损 JPEG，再编码只会更差。
                path = self.path_for(key, ORIGINAL, fmt=img.format)
                data = raw
            else:
                path = self.path_for(key, width)
                data = self._render(img, width)
            self._atomic_write(path, data)
            out_bytes += len(data)
            variants[width] = f"/img/{key[:2]}/{path.name}"

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
        """缩放并编码为 WebP（**只用于缩略档**）。

        只缩不放：源图比目标还窄时保持原尺寸，避免糊化。
        原图档不走这里——它是原样字节，见 :meth:`ensure`。
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
        for path in self._files_for(key):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    def _files_for(self, key: str) -> list[Path]:
        """该 key 在磁盘上的全部文件（含原图档，扩展名不定）。"""
        out: list[Path] = []
        for width in self._sizes:
            path = (self.original_path(key) if width == ORIGINAL
                    else self.path_for(key, width))
            if path is not None and path.exists():
                out.append(path)
        return out

    def total_bytes(self) -> int:
        """已占用的磁盘空间（含原图档——它的扩展名不是 .webp）。"""
        return sum(p.stat().st_size for p in self.root.rglob("*")
                   if p.is_file() and p.suffix != ".tmp")

    def orphans(self, known_keys: set[str]) -> list[Path]:
        """找出没有任何帖子引用的图片文件（供 ``169bt doctor`` 清理）。"""
        out = []
        if not self.root.exists():
            return out
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            if path.stem.rsplit("-", 1)[0] not in known_keys:
                out.append(path)
        return out
