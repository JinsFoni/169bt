"""cron 解析测试。

★ 全部离线、表驱动。cron 求下一个时刻是**纯函数**，因此可以穷举边界：
字段数不对、步长为 0、范围颠倒、月/日/周边界、闰年 2 月 29 日。

★ 时区断言是重点：cron 必须按 **UTC+8** 解释，与服务器本地时区无关。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bt169.source.cron import CronError, parse_cron
from bt169.source.parse import TZ_ARCHIVE

# ------------------------------------------------------------------ 正常解析


def test_every_minute() -> None:
    c = parse_cron("* * * * *")
    now = datetime(2026, 9, 19, 10, 30, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 19, 10, 31, tzinfo=TZ_ARCHIVE)


def test_next_is_strictly_after() -> None:
    """★ 严格大于 ``now``：否则整点触发会立刻重复触发一次。"""
    c = parse_cron("30 10 * * *")
    now = datetime(2026, 9, 19, 10, 30, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 20, 10, 30, tzinfo=TZ_ARCHIVE)


def test_seconds_are_truncated() -> None:
    """秒/微秒被抹掉——cron 最小粒度是分钟。"""
    c = parse_cron("* * * * *")
    now = datetime(2026, 9, 19, 10, 30, 45, 123456, tzinfo=TZ_ARCHIVE)
    got = c.next_after(now)
    assert got == datetime(2026, 9, 19, 10, 31, tzinfo=TZ_ARCHIVE)
    assert got.second == 0 and got.microsecond == 0


@pytest.mark.parametrize("expr,now,expected", [
    # 每 2 小时整点
    ("0 */2 * * *", (10, 30), (12, 0)),
    ("0 */2 * * *", (11, 59), (12, 0)),
    ("0 */2 * * *", (12, 0), (14, 0)),
    # 每 15 分钟
    ("*/15 * * * *", (10, 31), (10, 45)),
    ("*/15 * * * *", (10, 45), (11, 0)),
    # 单值
    ("30 14 * * *", (10, 0), (14, 30)),
    # 列表
    ("0 6,18 * * *", (10, 0), (18, 0)),
    ("0 6,18 * * *", (19, 0), (6, 0)),      # 跨天
    # 范围
    ("0 9-17 * * *", (18, 0), (9, 0)),      # 跨天
    # 范围 + 步长
    ("0 9-17/4 * * *", (10, 0), (13, 0)),
])
def test_time_fields(expr, now, expected) -> None:
    c = parse_cron(expr)
    base = datetime(2026, 9, 19, now[0], now[1], tzinfo=TZ_ARCHIVE)
    got = c.next_after(base)
    want = datetime(2026, 9, 19, expected[0], expected[1], tzinfo=TZ_ARCHIVE)
    if want <= base:
        want += timedelta(days=1)
    assert got == want


def test_rolls_to_next_day() -> None:
    c = parse_cron("0 6 * * *")
    now = datetime(2026, 9, 19, 7, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 20, 6, 0, tzinfo=TZ_ARCHIVE)


def test_rolls_to_next_month() -> None:
    c = parse_cron("0 0 1 * *")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 10, 1, 0, 0, tzinfo=TZ_ARCHIVE)


def test_rolls_to_next_year() -> None:
    c = parse_cron("0 0 1 1 *")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2027, 1, 1, 0, 0, tzinfo=TZ_ARCHIVE)


def test_month_field_skips_whole_months() -> None:
    """月字段受限时按整月跳，不会一天一天爬。"""
    c = parse_cron("0 0 1 3 *")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2027, 3, 1, 0, 0, tzinfo=TZ_ARCHIVE)


# --------------------------------------------------------------- 日/周语义


def test_weekday_only() -> None:
    """2026-09-19 是周六 → 下一个周一 09-21。"""
    c = parse_cron("0 0 * * 1")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 21, 0, 0, tzinfo=TZ_ARCHIVE)


def test_weekday_sunday_zero() -> None:
    c = parse_cron("0 0 * * 0")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 20, 0, 0, tzinfo=TZ_ARCHIVE)


def test_weekday_sunday_seven_is_alias() -> None:
    """★ cron 里 7 与 0 都是周日。"""
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert (parse_cron("0 0 * * 7").next_after(now)
            == parse_cron("0 0 * * 0").next_after(now))


def test_day_and_weekday_union_when_both_restricted() -> None:
    """★ Vixie cron 语义：日与周都受限时取**并集**。

    2026-09-19 是周六。表达式「1 号 或 周一」在它之后应命中
    09-21（周一），而不是等到 10-01（1 号）——并集取**最近**的一个。
    """
    c = parse_cron("0 0 1 * 1")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 21, 0, 0, tzinfo=TZ_ARCHIVE)


def test_day_and_weekday_union_reaches_day_of_month() -> None:
    """并集的另一半：非周一的「1 号」也要命中。

    ★ 这个用例真正区分**并集**与**交集**：
    2026-10-01 是周四（不是周一）。交集语义下它会被跳过，
    要等到 2027-02-01（那天恰好是周一）才命中；并集语义下 10-01 即命中。

    起点取 09-29（周二）——此时下一个周一 10-05 与下一个 1 号 10-01，
    并集应取更近的 10-01。
    """
    c = parse_cron("0 0 1 * 1")
    now = datetime(2026, 9, 29, 0, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 10, 1, 0, 0, tzinfo=TZ_ARCHIVE)


def test_day_star_means_intersect_with_weekday() -> None:
    """日是 ``*`` → 只看周。"""
    c = parse_cron("0 0 * * 1")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    got = c.next_after(now)
    assert got == datetime(2026, 9, 21, 0, 0, tzinfo=TZ_ARCHIVE)


def test_weekday_star_means_intersect_with_day() -> None:
    """周是 ``*`` → 只看日。"""
    c = parse_cron("0 0 25 * *")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2026, 9, 25, 0, 0, tzinfo=TZ_ARCHIVE)


# -------------------------------------------------------------------- 闰年


def test_leap_day_found_within_lookahead() -> None:
    """★ ``0 0 29 2 *`` 最坏要等 4 年——必须在向前搜索窗口内。

    2026 不是闰年，下一个 2 月 29 日是 2028-02-29。
    """
    c = parse_cron("0 0 29 2 *")
    now = datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2028, 2, 29, 0, 0, tzinfo=TZ_ARCHIVE)


def test_leap_day_just_after() -> None:
    c = parse_cron("0 0 29 2 *")
    now = datetime(2028, 2, 29, 0, 1, tzinfo=TZ_ARCHIVE)
    assert c.next_after(now) == datetime(2032, 2, 29, 0, 0, tzinfo=TZ_ARCHIVE)


# ------------------------------------------------------------------- 时区


def test_timezone_is_utc8_not_local() -> None:
    """★ cron 按 UTC+8 解释，与传入时刻的时区表示无关。

    北京时间 2026-09-20 02:00 == UTC 2026-09-19 18:00。
    """
    c = parse_cron("0 2 * * *")
    now_utc = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
    got = c.next_after(now_utc)
    # 结果换算回 UTC+8 必须是 09-20 02:00
    assert got.astimezone(TZ_ARCHIVE) == datetime(
        2026, 9, 20, 2, 0, tzinfo=TZ_ARCHIVE)


def test_result_is_always_utc8() -> None:
    """★ 返回值恒为 UTC+8——即使用户传的是别的时区。

    这样调用方（定时器）不需要再换算一次。
    """
    c = parse_cron("0 2 * * *")
    assert c.next_after(
        datetime(2026, 9, 19, 10, 0, tzinfo=TZ_ARCHIVE)).tzinfo is TZ_ARCHIVE
    assert c.next_after(
        datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)).tzinfo is TZ_ARCHIVE


# --------------------------------------------------------------- 非法表达式


@pytest.mark.parametrize("expr", [
    "",                      # 空
    "* * * *",               # 4 字段
    "* * * * * *",           # 6 字段（秒级不受支持）
    "60 * * * *",            # 分钟越界
    "* 24 * * *",            # 小时越界
    "* * 0 * *",             # 日从 1 开始
    "* * 32 * *",            # 日越界
    "* * * 0 *",             # 月从 1 开始
    "* * * 13 *",            # 月越界
    "* * * * 8",             # 周越界（7 是上限）
    "*/0 * * * *",           # 步长为 0 → 死循环风险
    "*/x * * * *",           # 步长非数字
    "1- * * * *",            # 范围缺终点
    "-5 * * * *",            # 范围缺起点
    "5-1 * * * *",           # 范围颠倒
    "a * * * *",             # 非数字
    "1,,2 * * * *",          # 空列表项
    "1/2/3 * * * *",         # 双重步长
])
def test_invalid_expression_raises(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)


def test_whitespace_tolerated() -> None:
    """多余空格不影响解析（用户在输入框里容易多打空格）。"""
    c = parse_cron("  0   */2   *  *  *  ")
    assert c.minutes == frozenset({0})
    assert c.hours == frozenset({0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22})


def test_expression_is_stripped() -> None:
    assert parse_cron("  * * * * *  ").expression == "* * * * *"


def test_cron_error_is_value_error() -> None:
    """★ 继承 ``ValueError``：调用方可以只 catch 一种。"""
    assert issubclass(CronError, ValueError)
