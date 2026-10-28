"""LTX 的 DTO 映射（字段对齐 4K，前端才能复用同一套卡片渲染）。"""

from __future__ import annotations

from bt169.collector.imagecache import LIGHTBOX_WIDTH
from bt169.models import LtxItem, LtxPostDTO, _LIGHTBOX_WIDTH, format_size


def item(**kw):
    base = dict(
        id=5252874674759445,
        number="SSIS-157",
        title="标题",
        section="LTX破解",
        publish_date="2026-10-25",
        size_bytes=3607826070,
        ed2k="ed2k://|file|x.mp4|1|AB|/",
        cover_url="https://img/cover.jpg",
        detail_url="https://javdb.com/v/x",
        rejected=0,
        created_at="2026-10-25T12:00:00+08:00",
        tg_sent_at=None,
        fetched_at="2026-10-25T13:00:00+08:00",
    )
    base.update(kw)
    return LtxItem(**base)


def test_lightbox_width_matches_imagecache():
    """★ DTO 里的灯箱宽度必须与图片缓存一致，否则灯箱取不到 1200 档。"""
    assert _LIGHTBOX_WIDTH == LIGHTBOX_WIDTH


def test_from_item_maps_to_post_shape():
    dto = LtxPostDTO.from_item(item()).to_dict()
    assert dto["tid"] == 5252874674759445
    assert dto["code"] == "SSIS-157"
    assert dto["post_date"] == "2026-10-25"
    assert dto["release_date"] == "2026-10-25"
    assert dto["size"] == "3.4GB"
    assert dto["status"] == "done"
    assert dto["actress"] is None
    assert dto["detail"] is None
    assert dto["emby_in_library"] is False


def test_cover_defaults_to_proxy_endpoint_with_two_widths():
    dto = LtxPostDTO.from_item(item()).to_dict()
    assert dto["cover"] == "/api/ltx/cover/5252874674759445"
    assert dto["cover_orig"] == f"/api/ltx/cover/5252874674759445?w={LIGHTBOX_WIDTH}"


def test_cover_none_when_no_source_image():
    dto = LtxPostDTO.from_item(item(cover_url=None)).to_dict()
    assert dto["cover"] is None
    assert dto["cover_orig"] is None


def test_tg_sent_at_is_passed_through():
    dto = LtxPostDTO.from_item(item(tg_sent_at="2026-10-26T09:00:00+08:00")).to_dict()
    assert dto["tg_sent_at"] == "2026-10-26T09:00:00+08:00"


def test_format_size_units():
    assert format_size(None) is None
    assert format_size(0) is None
    assert format_size(3607826070) == "3.4GB"
    assert format_size(700 * 1024 ** 2) == "700MB"


def test_actress_is_passed_through():
    """★ 卡片头一行的演员徽章直接吃 DTO 的这个字段（与 4K 同一套渲染）。"""
    dto = LtxPostDTO.from_item(item(actress="歌野こころ、足立友梨")).to_dict()
    assert dto["actress"] == "歌野こころ、足立友梨"


def test_actress_is_none_when_not_backfilled():
    dto = LtxPostDTO.from_item(item(actress=None, actress_at=None)).to_dict()
    assert dto["actress"] is None
