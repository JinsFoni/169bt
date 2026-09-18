"""RSS 发现层测试（C-1）。全部离线，用真实订阅 fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bt169.source.parse import (
    RssItem,
    parse_rss,
    parse_rss_date,
    tid_from_link,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------ tid_from_link


def test_tid_from_link_basic() -> None:
    assert tid_from_link(
        "https://169bt.com/forum.php?mod=viewthread&tid=3986000"
    ) == 3986000


def test_tid_from_link_entities_are_decoded() -> None:
    """RSS 里是 ``&amp;`` 转义形式，解析前必须解码。"""
    assert tid_from_link(
        "https://169bt.com/forum.php?mod=viewthread&amp;tid=3986000"
    ) == 3986000


def test_tid_from_link_param_order_independent() -> None:
    assert tid_from_link(
        "https://169bt.com/forum.php?tid=42&mod=viewthread"
    ) == 42


def test_tid_from_link_no_tid_returns_none() -> None:
    assert tid_from_link("https://169bt.com/forum.php?mod=forumdisplay&fid=192") is None


def test_tid_from_link_garbage_returns_none() -> None:
    assert tid_from_link("") is None
    assert tid_from_link("not a url") is None


def test_tid_from_link_non_numeric_returns_none() -> None:
    assert tid_from_link("https://x.com/forum.php?tid=abc") is None


# ------------------------------------------------------------ parse_rss_date


def test_parse_rss_date_utc_to_utc8() -> None:
    """实测：``pubDate`` 是 UTC，归档日期必须按 UTC+8 算（C-9 / 事实 #13）。"""
    assert parse_rss_date("Mon, 14 Sep 2026 11:33:59 +0000") == "2026-09-14"


def test_parse_rss_date_crosses_midnight() -> None:
    """UTC 15:00 = UTC+8 次日 23:00 → 日期必须跳一天。

    实测 20 条里没有跨日样本，但这是 C-9 的**存在理由**：
    UTC 16:00–23:59 发帖在 UTC+8 已是次日。
    """
    assert parse_rss_date("Mon, 14 Sep 2026 16:30:00 +0000") == "2026-09-15"


def test_parse_rss_date_boundary_just_before_midnight() -> None:
    """UTC 15:59:59 → UTC+8 23:59:59，仍是当天。"""
    assert parse_rss_date("Mon, 14 Sep 2026 15:59:59 +0000") == "2026-09-14"


def test_parse_rss_date_explicit_offset_respected() -> None:
    """带 +0800 的输入按它自己的时区算，不要再平移一次。"""
    assert parse_rss_date("Mon, 14 Sep 2026 23:30:00 +0800") == "2026-09-14"


def test_parse_rss_date_garbage_returns_none() -> None:
    assert parse_rss_date("") is None
    assert parse_rss_date("not a date") is None


# ------------------------------------------------------------ parse_rss


def test_parse_rss_real_feed_item_count() -> None:
    """实测：RSS 最多返回 20 条（事实 #1）。"""
    items = parse_rss(load("rss_fid192.xml"))
    assert len(items) == 20


def test_parse_rss_first_item_all_fields() -> None:
    items = parse_rss(load("rss_fid192.xml"))
    first = items[0]
    assert isinstance(first, RssItem)
    assert first.tid == 3986000
    assert first.post_date == "2026-09-14"
    assert "START-624" in first.title


def test_parse_rss_tids_are_unique() -> None:
    items = parse_rss(load("rss_fid192.xml"))
    tids = [i.tid for i in items]
    assert len(tids) == len(set(tids))


def test_parse_rss_is_newest_first() -> None:
    """RSS 顺序 = 时间降序；发现逻辑依赖这个前提。"""
    items = parse_rss(load("rss_fid192.xml"))
    dates = [i.post_date for i in items]
    assert dates == sorted(dates, reverse=True)


def test_parse_rss_matches_known_posts() -> None:
    """与库里已采集的 13 帖对得上（防解析偏移）。"""
    items = parse_rss(load("rss_fid192.xml"))
    by_tid = {i.tid: i for i in items}
    assert by_tid[3986000].post_date == "2026-09-14"
    assert "START-624" in by_tid[3986000].title
    # 前 9 帖是 09-01 之后同一批发帖；这里只验证 RSS 里存在的那些
    assert 3985999 in by_tid
    assert by_tid[3985999].post_date == "2026-09-14"


def test_parse_rss_skips_item_without_tid() -> None:
    """缺 tid 的条目直接跳过，不能让整个 feed 失败。"""
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>坏条目</title>
        <link>https://169bt.com/forum.php?mod=forumdisplay&amp;fid=192</link>
        <pubDate>Mon, 14 Sep 2026 11:00:00 +0000</pubDate>
      </item>
      <item>
        <title>好条目</title>
        <link>https://169bt.com/forum.php?mod=viewthread&amp;tid=123</link>
        <pubDate>Mon, 14 Sep 2026 10:00:00 +0000</pubDate>
      </item>
    </channel></rss>"""
    items = parse_rss(xml)
    assert [i.tid for i in items] == [123]


def test_parse_rss_skips_item_without_date() -> None:
    """缺 pubDate → 没有归档日期，跳过（不能拿今天顶替）。"""
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>无日期</title>
        <link>https://169bt.com/forum.php?mod=viewthread&amp;tid=9</link>
      </item>
    </channel></rss>"""
    assert parse_rss(xml) == []


def test_parse_rss_empty_and_garbage_return_empty_list() -> None:
    """★ 容错：feed 结构变了不能让轮询崩掉。"""
    assert parse_rss("") == []
    assert parse_rss("not xml at all") == []
    assert parse_rss("<rss><channel></channel></rss>") == []


def test_parse_rss_html_error_page_returns_empty() -> None:
    """★ 被 WAF 拦时返回 HTML 而不是 XML —— 必须安全退化为空。

    实测：``rss.php?fid=192`` 会返回 404 的 "Blocked By WAF" HTML 页面。
    """
    html = "<!doctype html><html><head><title>Blocked By WAF</title></head></html>"
    assert parse_rss(html) == []


def test_parse_rss_does_not_leak_ed2k_expectation() -> None:
    """事实 #2：description 被截断，不含 ed2k —— 解析层不该假装有。"""
    items = parse_rss(load("rss_fid192.xml"))
    assert all("ed2k://" not in i.title for i in items)
