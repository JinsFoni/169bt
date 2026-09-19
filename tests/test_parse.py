"""解析层测试。全部离线，用真实页面 fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bt169.source.parse import (
    extract_codes,
    extract_ed2k,
    is_login_required,
    is_thanks_required,
    normalize_date,
    parse_thread_detail,
    parse_thread_list,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------ normalize_date


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-9-4", "2026-09-04"),
        ("2026-09-04", "2026-09-04"),
        ("2026-12-31", "2026-12-31"),
        ("1999-1-1", "1999-01-01"),
    ],
)
def test_normalize_date_pads(raw, expected):
    """实测论坛混用补零/不补零两种写法，必须统一。"""
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "2026-13-01", "2026-02-30", "26-1-1"])
def test_normalize_date_rejects_invalid(raw):
    assert normalize_date(raw) is None


# ------------------------------------------------------------ extract_codes


def test_extract_codes_does_not_confuse_prefix():
    """E-5：START-62 不得命中 START-624。"""
    assert extract_codes("START-624 配送中NTR") == {"START-624"}
    assert extract_codes("START-62") == {"START-62"}
    assert extract_codes("START-6240") == {"START-6240"}


def test_extract_codes_uppercases():
    assert extract_codes("start-624") == {"START-624"}


def test_extract_codes_ignores_unhyphenated():
    assert extract_codes("START624") == set()


def test_extract_codes_from_filename():
    assert extract_codes("s169bbs.com@START-624_[4K].mkv") == {"START-624"}


# ------------------------------------------------------------ extract_ed2k


def test_extract_ed2k():
    html = "看这个 ed2k://|file|s169bbs.com@START-624_[4K].mkv|7515146184|0B17E95C|/ 吧"
    assert extract_ed2k(html) == (
        "ed2k://|file|s169bbs.com@START-624_[4K].mkv|7515146184|0B17E95C|/"
    )


def test_extract_ed2k_absent():
    assert extract_ed2k("<div>没有链接</div>") is None


# ------------------------------------------------------------ 登录/感谢判定


def test_login_required_false_when_footer_mentions_login():
    """★ 回归：每个页面底部的快速回复框都有「您需要登录后才可以发帖」。

    实测 page3 因此被误判为会话失效。真正的失效判定必须同时「没有 ed2k」。
    """
    html = '<div class="pt hm">您需要登录后才可以发帖</div>'
    assert is_login_required(html) is False


def test_login_required_false_when_only_footer_phrase():
    """仅有底部固定文案不算失效（实测每个页面都有）。"""
    html = '<div class="pt hm">您需要登录后才可以发帖</div>'
    assert is_login_required(html) is False


def test_login_required_false_when_vague_hint_only():
    """只有「您需要登录」四个字、无登录墙标志 → 不算失效。

    宁可漏判（退化到下轮再检）也不能误判——误判会触发重登录、
    白白消耗登录额度（5 次/900 秒）。
    """
    assert is_login_required("您需要登录 <p>内容被隐藏</p>") is False


def test_login_required_true_on_login_form():
    html = '<form id="loginform_abc">您需要登录</form>'
    assert is_login_required(html) is True


def test_login_required_true_on_permission_error():
    html = "抱歉，本帖要求阅读权限高于 20 才能浏览"
    assert is_login_required(html) is True


def test_login_required_false_when_ed2k_present():
    html = "您需要登录 ed2k://|file|a.mkv|1|AB|/"
    assert is_login_required(html) is False


def test_thanks_required():
    assert is_thanks_required('<div class="locked">回复可见</div>') is True
    assert is_thanks_required("<div>正常内容</div>") is False


# ------------------------------------------------------------ 列表页


def test_list_page1_row_count():
    """实测每页 28 行。"""
    rows = parse_thread_list(load("forumdisplay_p1.html"))
    assert len(rows) == 28


@pytest.mark.parametrize(
    "page", ["forumdisplay_p1.html", "forumdisplay_p2.html", "forumdisplay_p3.html"]
)
def test_list_all_rows_have_dates(page):
    """★ 回归：老帖用纯文本日期、新帖用 title 属性。

    早期版本只认 title 属性，导致 page2 起**所有行日期为空**。
    """
    rows = parse_thread_list(load(page))
    assert all(r.post_date for r in rows), [
        r.tid for r in rows if not r.post_date
    ]


@pytest.mark.parametrize(
    "page", ["forumdisplay_p1.html", "forumdisplay_p2.html", "forumdisplay_p3.html"]
)
def test_list_sorted_descending_by_post_date(page):
    """★ 关键前提：论坛按**发帖时间**降序返回。

    回填靠这个前提做「遇到早于 from 的日期就停」的提前退出。
    若此假设不成立，提前退出会漏帖。
    """
    dates = [r.post_date for r in parse_thread_list(load(page))]
    assert all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))


def test_list_dates_are_normalized():
    """混合写法必须都被归一化。"""
    for page in ("forumdisplay_p1.html", "forumdisplay_p2.html"):
        for r in parse_thread_list(load(page)):
            assert len(r.post_date) == 10 and r.post_date[4] == "-", r.post_date


def test_list_first_row_known_sample():
    rows = parse_thread_list(load("forumdisplay_p1.html"))
    assert rows[0].tid == 3986000
    assert rows[0].post_date == "2026-09-14"
    assert "START-624" in rows[0].title


def test_list_reply_counts_parsed():
    rows = parse_thread_list(load("forumdisplay_p1.html"))
    assert rows[0].reply_count is not None and rows[0].reply_count >= 0


def test_list_empty_html():
    assert parse_thread_list("<html><body>没有帖子</body></html>") == []


def test_list_skips_malformed_row():
    """id 非数字的行应被跳过，而不是让整个解析崩掉。"""
    html = '<tbody id="normalthread_abc"><tr><th>x</th></tr></tbody>'
    assert parse_thread_list(html) == []


# ------------------------------------------------------------ 详情页


def test_detail_locked_sample():
    """真实锁定帖（tid=3986000），字段值与实测样本逐一比对。"""
    d = parse_thread_detail(load("viewthread_locked.html"), 3986000)
    assert d.tid == 3986000
    assert d.code == "START-624"
    assert d.actress == "本庄鈴"
    assert d.release_date == "2026-09-17"
    assert d.size is not None and d.size.startswith("7GB")
    assert d.cover_img == "https://www.imgccc.com/2026/09/14/7f7fa7216d7a2.jpg"
    assert d.detail_img == "https://www.imgccc.com/2026/09/14/c37468d0c724d.jpg"


def test_detail_locked_has_no_ed2k_and_is_locked():
    """未感谢时 ed2k 不可见，且必须标记 locked。"""
    d = parse_thread_detail(load("viewthread_locked.html"), 3986000)
    assert d.ed2k is None
    assert d.locked is True


def test_detail_tolerates_empty_html():
    """字段全缺失时不抛异常（风险表「字段格式变动」）。"""
    d = parse_thread_detail("<html><body></body></html>", 12345)
    assert d.tid == 12345
    assert d.code is None
    assert d.ed2k is None
    assert d.cover_img is None
    assert d.locked is False


def test_detail_extracts_ed2k_when_present():
    html = """
    <html><body>
    <h1>[4K] START-624 标题</h1>
    <td class="t_f">[影片名称]：[4K] START-624 标题<br />
    出演者：本庄鈴<br />商品発売日：2026/09/17<br />[影片大小]：7GB<br />
    ed2k://|file|s169bbs.com@START-624_[4K].mkv|7515146184|0B17E95C|/
    <img src="https://www.imgccc.com/2026/09/14/aaa.jpg" />
    </td></body></html>
    """
    d = parse_thread_detail(html, 1)
    assert d.ed2k is not None and d.ed2k.startswith("ed2k://")
    assert d.locked is False
    assert d.code == "START-624"
    assert d.actress == "本庄鈴"
    assert d.release_date == "2026-09-17"


def test_detail_single_image_gives_no_detail_img():
    html = '<td class="t_f"><img src="https://www.imgccc.com/a/1.jpg" /></td>'
    d = parse_thread_detail(html, 1)
    assert d.cover_img == "https://www.imgccc.com/a/1.jpg"
    assert d.detail_img is None


def test_detail_ignores_duplicate_images():
    html = (
        '<td class="t_f">'
        '<img src="https://www.imgccc.com/a/1.jpg" />'
        '<img src="https://www.imgccc.com/a/1.jpg" />'
        "</td>"
    )
    d = parse_thread_detail(html, 1)
    assert d.detail_img is None


def test_detail_cleans_html_entities():
    html = '<td class="t_f">出演者：&nbsp;本庄鈴&nbsp;</td>'
    d = parse_thread_detail(html, 1)
    assert d.actress == "本庄鈴"


# ------------------------------------------------------------ 字段值后的 @注解


def test_detail_size_strips_annotation():
    """★ 站点在字段值后追加 ``@注解``，只保留值本身。

    原文是 ``[影片大小]：7GB@NO Watermark``。旧测试只断言
    ``startswith("7GB")`` —— 注解一起被存进库也照样通过，
    于是 20 行 ``XGB@NO Watermark`` 就这样进了生产库。
    """
    d = parse_thread_detail(load("viewthread_locked.html"), 3986000)
    assert d.size == "7GB"


@pytest.mark.parametrize("raw,expected", [
    ("7GB@NO Watermark", "7GB"),
    ("14GB@NO Watermark", "14GB"),
    ("1天@Please Seed", "1天"),
    ("有码@无水印", "有码"),
    # 没有注解 → 原样返回
    ("7GB", "7GB"),
    # 注解里还有 @ → 只切第一个
    ("7GB@A@B", "7GB"),
    # 只有空白 → None（与 _clean 的约定一致）
    ("   ", None),
])
def test_strip_annotation(raw, expected):
    """``_strip_annotation`` 的直接单元测试。"""
    from bt169.source.parse import _strip_annotation

    assert _strip_annotation(raw) == expected


def test_strip_annotation_none():
    from bt169.source.parse import _strip_annotation

    assert _strip_annotation(None) is None
