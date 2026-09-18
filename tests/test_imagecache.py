"""图片本地化测试。全部离线（假 fetch + 程序生成的图）。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from bt169.collector.imagecache import (
    CARD_WIDTH,
    LIGHTBOX_WIDTH,
    ImageCache,
    ImageError,
)


def png(width: int, height: int, color=(200, 30, 40)) -> bytes:
    """生成一张纯色 PNG。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "PNG")
    return buf.getvalue()


class FakeFetch:
    """记录调用次数的假下载器。"""

    def __init__(self, payload: bytes | dict[str, bytes]) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if isinstance(self.payload, dict):
            return self.payload[url]
        return self.payload


def make_cache(tmp_path: Path, payload, **kw) -> tuple[ImageCache, FakeFetch]:
    fetch = FakeFetch(payload)
    return ImageCache(tmp_path / "images", fetch=fetch, **kw), fetch


# ------------------------------------------------------------ 键与路径


def test_key_is_stable_and_url_derived():
    a = ImageCache.key_for("https://img.example/x.jpg")
    assert a == ImageCache.key_for("https://img.example/x.jpg")
    assert a != ImageCache.key_for("https://img.example/y.jpg")
    assert len(a) == 16


def test_same_url_dedupes_to_same_path(tmp_path):
    """★ 同图多帖引用只存一份。"""
    c, _ = make_cache(tmp_path, png(800, 600))
    a = c.ensure("https://img.example/same.jpg")
    b = c.ensure("https://img.example/same.jpg")
    assert a.key == b.key
    assert a.variants == b.variants


def test_path_buckets_by_key_prefix(tmp_path):
    c, _ = make_cache(tmp_path, png(800, 600))
    cached = c.ensure("https://img.example/x.jpg")
    p = c.path_for(cached.key, CARD_WIDTH)
    assert p.parent.name == cached.key[:2]
    assert p.name == f"{cached.key}-{CARD_WIDTH}.webp"


# ------------------------------------------------------------ 下载与转码


def test_ensure_downloads_and_converts(tmp_path):
    c, fetch = make_cache(tmp_path, png(2184, 1542))
    cached = c.ensure("https://img.example/big.jpg")

    assert fetch.calls == ["https://img.example/big.jpg"]
    assert cached.bytes_in > 0
    assert set(cached.variants) == {CARD_WIDTH, LIGHTBOX_WIDTH}

    with Image.open(c.path_for(cached.key, CARD_WIDTH)) as im:
        assert im.format == "WEBP"
        assert im.size == (CARD_WIDTH, round(1542 * CARD_WIDTH / 2184))

    with Image.open(c.path_for(cached.key, LIGHTBOX_WIDTH)) as im:
        assert im.size == (LIGHTBOX_WIDTH, round(1542 * LIGHTBOX_WIDTH / 2184))


def test_variant_url_shape(tmp_path):
    c, _ = make_cache(tmp_path, png(800, 600))
    cached = c.ensure("https://img.example/x.jpg")
    assert cached.variants[CARD_WIDTH] == f"/img/{cached.key[:2]}/{cached.key}-600.webp"


def test_second_ensure_makes_no_request(tmp_path):
    """★ 已缓存就不该再下载。"""
    c, fetch = make_cache(tmp_path, png(800, 600))
    c.ensure("https://img.example/x.jpg")
    c.ensure("https://img.example/x.jpg")
    assert len(fetch.calls) == 1


def test_force_redownloads(tmp_path):
    c, fetch = make_cache(tmp_path, png(800, 600))
    c.ensure("https://img.example/x.jpg")
    c.ensure("https://img.example/x.jpg", force=True)
    assert len(fetch.calls) == 2


def test_does_not_upscale_small_images(tmp_path):
    """★ 源图比目标窄时保持原尺寸，不放大（放大只会更糊）。"""
    c, _ = make_cache(tmp_path, png(300, 200))
    cached = c.ensure("https://img.example/small.jpg")
    with Image.open(c.path_for(cached.key, CARD_WIDTH)) as im:
        assert im.size == (300, 200)


def test_compresses_significantly(tmp_path):
    """真实照片才有压缩空间；纯色图不具代表性，这里只验能变小。"""
    rng = __import__("random").Random(7)
    buf = io.BytesIO()
    # 生成有噪声的图（纯色 PNG 转 WebP 反而可能变大）
    img = Image.new("RGB", (1200, 800))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(1200 * 800)])
    img.save(buf, "PNG")
    c, _ = make_cache(tmp_path, buf.getvalue())
    cached = c.ensure("https://img.example/noise.jpg")
    assert cached.bytes_out < cached.bytes_in


# ------------------------------------------------------------ 错误处理


def test_html_error_page_rejected(tmp_path):
    """★ 图床出错返回 HTML 时不能存成「成功的」坏图。"""
    c, _ = make_cache(tmp_path, b"<html><body>404 Not Found</body></html>")
    with pytest.raises(ImageError, match="不是有效图片"):
        c.ensure("https://img.example/broken.jpg")


def test_empty_response_rejected(tmp_path):
    c, _ = make_cache(tmp_path, b"")
    with pytest.raises(ImageError, match="空响应"):
        c.ensure("https://img.example/empty.jpg")


def test_oversized_image_rejected(tmp_path):
    c, _ = make_cache(tmp_path, b"x" * (21 * 1024 * 1024))
    with pytest.raises(ImageError, match="过大"):
        c.ensure("https://img.example/huge.jpg")


def test_download_failure_wrapped(tmp_path):
    def boom(url):
        raise ConnectionError("图床挂了")

    c = ImageCache(tmp_path / "images", fetch=boom)
    with pytest.raises(ImageError, match="下载失败"):
        c.ensure("https://img.example/x.jpg")


def test_failed_download_leaves_no_partial_file(tmp_path):
    """★ 失败后不能留下会被 has() 误判为已缓存的半成品。"""
    c, _ = make_cache(tmp_path, b"not an image")
    with pytest.raises(ImageError):
        c.ensure("https://img.example/x.jpg")
    assert c.has("https://img.example/x.jpg") is False
    assert list((tmp_path / "images").rglob("*.webp")) == []


# ------------------------------------------------------------ ensure_post


class FakePost:
    def __init__(self, tid, cover, detail):
        self.tid = tid
        self.cover_img = cover
        self.detail_img = detail


def test_ensure_post_caches_both_images(tmp_path):
    c, fetch = make_cache(tmp_path, {
        "https://img.example/c.jpg": png(800, 600),
        "https://img.example/d.jpg": png(900, 700),
    })
    out = c.ensure_post(FakePost(1, "https://img.example/c.jpg",
                                 "https://img.example/d.jpg"))
    assert set(out) == {"cover", "detail"}
    assert len(fetch.calls) == 2


def test_ensure_post_skips_missing(tmp_path):
    c, fetch = make_cache(tmp_path, png(800, 600))
    out = c.ensure_post(FakePost(1, None, "https://img.example/d.jpg"))
    assert set(out) == {"detail"}


def test_ensure_post_does_not_raise_on_failure(tmp_path):
    """★ 图片失败不该让整帖采集失败。"""
    c, _ = make_cache(tmp_path, b"<html>nope</html>")
    out = c.ensure_post(FakePost(1, "https://img.example/c.jpg", None))
    assert out == {}


# ------------------------------------------------------------ 删除与清理


def test_delete_removes_all_variants(tmp_path):
    c, _ = make_cache(tmp_path, png(800, 600))
    url = "https://img.example/x.jpg"
    cached = c.ensure(url)
    assert c.has(url)

    removed = c.delete(url)
    assert removed == len(cached.variants)
    assert c.has(url) is False
    assert not c.path_for(cached.key, CARD_WIDTH).exists()


def test_delete_is_idempotent(tmp_path):
    c, _ = make_cache(tmp_path, png(800, 600))
    assert c.delete("https://img.example/never.jpg") == 0


def test_total_bytes(tmp_path):
    c, _ = make_cache(tmp_path, png(800, 600))
    assert c.total_bytes() == 0
    c.ensure("https://img.example/x.jpg")
    assert c.total_bytes() > 0


def test_orphans_finds_unreferenced(tmp_path):
    c, _ = make_cache(tmp_path, {
        "https://img.example/keep.jpg": png(800, 600),
        "https://img.example/drop.jpg": png(800, 600),
    })
    kept = c.ensure("https://img.example/keep.jpg")
    c.ensure("https://img.example/drop.jpg")

    orphans = c.orphans({kept.key})
    assert len(orphans) == 2                       # drop 的两个档位
    assert all(o.stem.rsplit("-", 1)[0] != kept.key for o in orphans)


# ------------------------------------------------------------ 删除联动（API 层）
#
# 这一组测试验证「硬删除帖子时图片的引用计数」。
# 放在这里而不是 test_api_*：断言的是图片文件的实际存留，
# 与 ImageCache 的行为耦合最紧。


@pytest.fixture()
def img_app(db, box, tmp_path):
    """带真实 ImageCache 的 app（图片目录指向 tmp_path）。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app
    from bt169.repo.posts import PostRepo

    image_dir = tmp_path / "images"
    image_dir.mkdir()
    app = create_app(db, box=box, ui_dir=None, image_dir=image_dir)
    with TestClient(app) as c:
        c.image_dir = image_dir            # type: ignore[attr-defined]
        c.posts = PostRepo(db)             # type: ignore[attr-defined]
        yield c


def seed(cache: ImageCache, url: str, *, tid: int, posts) -> str:
    """造一张本地图 + 一行引用它的帖子，返回本地路径。"""
    cached = cache.ensure(url)
    posts.upsert_collected(
        tid=tid, title=f"T{tid}", code=None, actress=None, release_date=None,
        size=None, cover_img=url, detail_img=None,
        ed2k="ed2k://|file|a|1|AB|/", post_date="2026-09-14", status="done",
        cover_local=cached.variants[600],
    )
    return cached.variants[600]


def test_delete_removes_image_files(img_app):
    cache = ImageCache(img_app.image_dir, fetch=lambda u: png(800, 600))
    url = "https://img.example/only.jpg"
    local = seed(cache, url, tid=1, posts=img_app.posts)
    assert (img_app.image_dir / local.removeprefix("/img/")).exists()

    assert img_app.delete("/api/posts/1").status_code == 204
    assert not (img_app.image_dir / local.removeprefix("/img/")).exists()


def test_delete_keeps_image_still_referenced(img_app):
    """★ 引用计数：另一帖还在用同一张图 → 不能删文件。"""
    cache = ImageCache(img_app.image_dir, fetch=lambda u: png(800, 600))
    url = "https://img.example/shared.jpg"
    local = seed(cache, url, tid=1, posts=img_app.posts)
    seed(cache, url, tid=2, posts=img_app.posts)      # 第二帖引用同一 URL

    assert img_app.delete("/api/posts/1").status_code == 204

    path = img_app.image_dir / local.removeprefix("/img/")
    assert path.exists(), "还有帖子引用该图，不该删文件"

    # 删掉最后一帖 → 这时才能删
    assert img_app.delete("/api/posts/2").status_code == 204
    assert not path.exists()


def test_delete_without_local_images_is_fine(img_app):
    """没有本地图的帖子也能正常删除（不能因为找不到文件而 500）。"""
    img_app.posts.upsert_collected(
        tid=9, title="t", code=None, actress=None, release_date=None,
        size=None, cover_img="https://img.example/never-downloaded.jpg",
        detail_img=None, ed2k=None, post_date="2026-09-14", status="pending",
    )
    assert img_app.delete("/api/posts/9").status_code == 204


def test_delete_missing_post_404(img_app):
    assert img_app.delete("/api/posts/999").status_code == 404


def test_beacon_endpoint_also_cleans_images(img_app):
    """★ sendBeacon 兜底端点语义必须与 DELETE 等价（否则关页面会留孤儿图）。"""
    cache = ImageCache(img_app.image_dir, fetch=lambda u: png(800, 600))
    url = "https://img.example/beacon.jpg"
    local = seed(cache, url, tid=5, posts=img_app.posts)

    assert img_app.post("/api/posts/5/delete").status_code == 204
    assert not (img_app.image_dir / local.removeprefix("/img/")).exists()


def test_dto_serves_local_path_after_seed(img_app):
    """★ 端到端：本地化后 API 返回本地路径，前端无需改动。"""
    cache = ImageCache(img_app.image_dir, fetch=lambda u: png(800, 600))
    seed(cache, "https://img.example/x.jpg", tid=7, posts=img_app.posts)

    body = img_app.get("/api/posts?date=2026-09-14").json()
    assert body[0]["cover"].startswith("/img/")


def test_img_mount_serves_the_file(img_app):
    """★ /img 静态分发真的能取到文件。"""
    cache = ImageCache(img_app.image_dir, fetch=lambda u: png(800, 600))
    local = seed(cache, "https://img.example/x.jpg", tid=8, posts=img_app.posts)

    r = img_app.get(local)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert len(r.content) > 0


# ------------------------------------------------------------ /img 挂载（app 层）
#
# ★ 这一组是回归测试。两个真实 bug 都是「测试全绿但线上坏」：
#   1. /img 只在目录已存在时才挂载 → 全新安装 404 到重启
#   2. 注释声称 immutable 缓存，实际没有任何缓存头
#   两者都不是单测能发现的——必须真的把 app 装起来发请求。


def test_img_mounted_even_when_dir_missing(tmp_path, db, box):
    """★ 回归：全新安装时图片目录尚不存在，/img 仍必须可用。

    ImageCache 是首次下载才懒建目录的，所以启动时目录通常不存在。
    """
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    missing = tmp_path / "not-yet" / "images"
    assert not missing.exists()

    app = create_app(db, box=box, ui_dir=None, image_dir=missing)
    assert missing.is_dir(), "app 装配时必须把目录建出来"

    # 真放一张图进去，确认能取到（而不是只挂了个空目录）
    cache = ImageCache(missing, fetch=lambda u: png(800, 600))
    local = cache.ensure("https://img.example/x.jpg").variants[600]
    with TestClient(app) as c:
        r = c.get(local)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"


def test_img_sets_immutable_cache_header(tmp_path, db, box):
    """★ 回归：本地化图片必须带长缓存头（文件名内容派生，可永久缓存）。"""
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    image_dir = tmp_path / "images"
    app = create_app(db, box=box, ui_dir=None, image_dir=image_dir)
    cache = ImageCache(image_dir, fetch=lambda u: png(800, 600))
    local = cache.ensure("https://img.example/y.jpg").variants[600]

    with TestClient(app) as c:
        r = c.get(local)
    cc = r.headers["cache-control"]
    assert "immutable" in cc
    assert "max-age=31536000" in cc


def test_ui_static_does_not_get_immutable_header(tmp_path, db, box):
    """★ 反向断言：前端文件**不能**带 immutable。

    前端 HTML/JS/CSS 文件名不含哈希，用长缓存会导致改完代码刷新看不到。
    """
    from fastapi.testclient import TestClient

    from bt169.api.app import create_app

    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<html>hi</html>", encoding="utf-8")

    app = create_app(db, box=box, ui_dir=ui_dir, image_dir=None)
    with TestClient(app) as c:
        r = c.get("/index.html")
    assert r.status_code == 200
    assert "immutable" not in r.headers.get("cache-control", "")
