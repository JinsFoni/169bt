"""cron 表达式解析与求下次触发时刻。

★ **为什么自写而不引入 ``croniter``**：我们只需要一个纯函数
「给定 5 字段 cron + 当前时刻 → 下一个触发时刻」。``croniter`` 会连带
拉进 ``python-dateutil``（两个新依赖），而它的额外能力（秒级字段、
``L``/``W``/``#`` 扩展）在本项目用不到。项目已确立零依赖倾向
（ADR-15 拒绝 ``aiosqlite``、ADR-17 拒绝 ``argon2``）。

★ **时区恒为 ``TZ_ARCHIVE``（UTC+8）**，不是服务器本地时区：
用户填 ``0 2 * * *`` 想的是「北京时间凌晨 2 点」，而服务可能跑在
UTC 的容器里。UTC+8 无夏令时，因此不存在「某天的 2:30 不存在」
这类 DST 边界问题——这让 ``next_after`` 可以简单地在日历上走。

支持的语法（标准 5 字段：分 时 日 月 周）：

- ``*``              任意值
- ``5``              单值
- ``1,3,5``          列表
- ``1-5``            范围
- ``*/15``           步长
- ``1-30/5``         范围 + 步长
- ``0`` 或 ``7``     周日（周字段）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bt169.source.parse import TZ_ARCHIVE

__all__ = ["CronError", "Cron", "parse_cron", "MAX_LOOKAHEAD_DAYS"]

#: 各字段的取值范围（闭区间）。周字段允许 7 作为「周日」的别名。
FIELDS = (
    ("分钟", 0, 59),
    ("小时", 0, 23),
    ("日", 1, 31),
    ("月", 1, 12),
    ("周", 0, 7),
)

#: ``next_after`` 最多向前找多少天。4 年足以跨过任意闰年组合
#: （最坏情况 ``0 0 29 2 *`` 需要等 4 年），找不到即视为不可达。
MAX_LOOKAHEAD_DAYS = 366 * 4 + 1


class CronError(ValueError):
    """cron 表达式非法。"""


@dataclass(frozen=True)
class Cron:
    """已解析的 cron 表达式。"""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: 「日」与「周」是否都是 ``*``。Vixie cron 的经典语义：
    #: 两者都受限时取**并集**，否则取交集。见 ``_day_matches``。
    day_is_star: bool
    weekday_is_star: bool

    def next_after(self, now: datetime) -> datetime | None:
        """求 ``now`` **之后**（严格大于）的下一个触发时刻。

        返回值保留 ``now`` 的时区。找不到（理论上只在不可达表达式下
        发生）返回 ``None``。

        ★ 秒与微秒会被抹掉——cron 的最小粒度是分钟。

        ★ **先把 ``now`` 换算到 ``TZ_ARCHIVE``（UTC+8）再走日历**：
          cron 字段按北京时间解释。若直接用传入时刻的时区（可能是 UTC），
          ``0 2 * * *`` 会算成 UTC 02:00 = 北京 10:00，整整偏 8 小时。
        """
        # 从下一分钟开始找：cron 的语义是「在整分钟触发」。
        cand = (now.astimezone(TZ_ARCHIVE) + timedelta(minutes=1)).replace(
            second=0, microsecond=0)

        for _ in range(MAX_LOOKAHEAD_DAYS):
            if cand.month not in self.months:
                # 整月跳过：直接跳到下个月 1 号，省掉最多 30 次无用迭代。
                cand = _next_month(cand)
                continue
            if self._day_matches(cand):
                hit = self._time_on(cand)
                if hit is not None:
                    return hit
            # 当天没有可用时刻 → 跳到次日 00:00。
            cand = (cand + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        return None

    # ------------------------------------------------------------- 内部

    def _time_on(self, day: datetime) -> datetime | None:
        """在 ``day`` 这一天里找第一个满足时/分的时刻（不早于 ``day``）。"""
        for hour in sorted(self.hours):
            if hour < day.hour:
                continue
            if hour > day.hour:
                # 该小时整点即最早可能——取最小分钟。
                return day.replace(hour=hour, minute=min(self.minutes))
            for minute in sorted(self.minutes):
                if minute >= day.minute:
                    return day.replace(hour=hour, minute=minute)
        return None

    def _day_matches(self, day: datetime) -> bool:
        """日/月/周三者是否匹配。

        ★ **Vixie cron 的经典语义**：当「日」与「周」**都不是** ``*`` 时，
          两者取**并集**（满足其一即可）；否则取交集。这不是我们的发明，
          是 cron 的历史行为——照做才符合用户预期。
        """
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        # Python: Monday=0..Sunday=6；cron: Sunday=0..Saturday=6。
        dow_ok = ((day.weekday() + 1) % 7) in self.weekdays

        if self.day_is_star and self.weekday_is_star:
            return True
        if self.day_is_star:
            return dow_ok
        if self.weekday_is_star:
            return dom_ok
        return dom_ok or dow_ok


def _next_month(dt: datetime) -> datetime:
    """跳到下个月 1 号 00:00。"""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0)


def parse_cron(expression: str) -> Cron:
    """解析 5 字段 cron 表达式。非法时抛 :class:`CronError`。

    ★ 抛错即**不落库**：保证「能存进去的表达式一定能跑」。
    """
    parts = expression.split()
    if len(parts) != 5:
        raise CronError(
            f"cron 表达式需要 5 个字段（分 时 日 月 周），"
            f"实际 {len(parts)} 个：{expression!r}"
        )

    values = [
        _parse_field(raw, name, lo, hi)
        for raw, (name, lo, hi) in zip(parts, FIELDS)
    ]
    minutes, hours, days, months, weekdays = values

    # 周字段把 7 归一成 0（两者都是周日）。
    weekdays = frozenset(0 if w == 7 else w for w in weekdays)

    return Cron(
        # ★ 归一成单空格分隔：用户在输入框里容易多打空格，
        #   不折叠会让同一个表达式在库里出现多种写法。
        expression=" ".join(parts),
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        day_is_star=parts[2] == "*",
        weekday_is_star=parts[4] == "*",
    )


def _parse_field(raw: str, name: str, lo: int, hi: int) -> frozenset[int]:
    """解析单个字段，返回取值集合。"""
    if not raw:
        raise CronError(f"{name}字段为空")

    out: set[int] = set()
    for piece in raw.split(","):
        out |= _parse_piece(piece, name, lo, hi)
    if not out:
        raise CronError(f"{name}字段没有任何取值：{raw!r}")
    return frozenset(out)


def _parse_piece(piece: str, name: str, lo: int, hi: int) -> set[int]:
    """解析 ``*`` / ``a`` / ``a-b`` / ``*/n`` / ``a-b/n`` 中的一段。"""
    if not piece:
        raise CronError(f"{name}字段有空的列表项")

    step = 1
    if "/" in piece:
        base, _, step_raw = piece.partition("/")
        if "/" in step_raw:
            raise CronError(f"{name}字段步长写法非法：{piece!r}")
        if not step_raw.isdigit():
            raise CronError(f"{name}字段步长必须是正整数：{piece!r}")
        step = int(step_raw)
        # ★ 步长 0 会让 range 抛 ValueError，也会让「每 0 分钟」无意义。
        if step <= 0:
            raise CronError(f"{name}字段步长必须大于 0：{piece!r}")
    else:
        base = piece

    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        start_raw, _, end_raw = base.partition("-")
        start = _to_int(start_raw, name, lo, hi)
        end = _to_int(end_raw, name, lo, hi)
        if start > end:
            raise CronError(f"{name}字段范围起点大于终点：{piece!r}")
    else:
        start = end = _to_int(base, name, lo, hi)

    return set(range(start, end + 1, step))


def _to_int(raw: str, name: str, lo: int, hi: int) -> int:
    """把字段值转成整数并做范围检查。"""
    if not raw.isdigit():
        raise CronError(f"{name}字段不是数字：{raw!r}")
    value = int(raw)
    if not (lo <= value <= hi):
        raise CronError(f"{name}字段超出范围 {lo}-{hi}：{value}")
    return value
