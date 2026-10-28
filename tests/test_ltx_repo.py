"""LTX 镜像表读写（``repo/ltx.py``）。"""

from __future__ import annotations

from bt169.repo.ltx import LtxRepo
from bt169.repo.posts import now_iso


def row(tid, *, date="2026-10-25", number="SSIS-157", rejected=0,
        created="2026-10-25T12:00:00+08:00", size=3607826070):
    return {
        "id": tid,
        "number": number,
        "title": f"标题 {tid}",
        "section": "LTX破解",
        "publish_date": date,
        "size_bytes": size,
        "ed2k": f"ed2k://|file|{tid}.mp4|1|AB|/",
        "cover_url": "https://img/c.jpg",
        "detail_url": "https://javdb.com/v/x",
        "rejected": rejected,
        "created_at": created,
    }


def test_upsert_then_list_by_date(db):
    repo = LtxRepo(db)
    assert repo.upsert_many([row(1), row(2)], now_iso()) == 2

    items = repo.list_by_date("2026-10-25")
    assert {i.id for i in items} == {1, 2}
    assert items[0].publish_date == "2026-10-25"
    assert items[0].ed2k.startswith("ed2k://")


def test_upsert_is_idempotent(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    repo.upsert_many([row(1)], now_iso())
    assert repo.count() == 1


def test_upsert_keeps_tg_sent_at(db):
    """★ 同步绝不能清掉「已转发」标记——否则每次同步后都会重复转发。"""
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    repo.mark_tg_sent(1, "2026-10-26T10:00:00+08:00")

    repo.upsert_many([row(1)], now_iso())
    assert repo.get(1).tg_sent_at == "2026-10-26T10:00:00+08:00"


def test_upsert_updates_changed_fields(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    changed = row(1, number="SSIS-999")
    repo.upsert_many([changed], now_iso())
    assert repo.get(1).number == "SSIS-999"


def test_list_dates_is_ascending_with_counts(db):
    repo = LtxRepo(db)
    repo.upsert_many(
        [row(1, date="2026-10-24"), row(2, date="2026-10-24"), row(3, date="2026-10-25")],
        now_iso(),
    )
    assert repo.list_dates() == [("2026-10-24", 2), ("2026-10-25", 1)]


def test_list_dates_excludes_rejected(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1, rejected=1)], now_iso())
    assert repo.list_dates() == []
    assert repo.count() == 0


def test_list_by_date_is_newest_first(db):
    repo = LtxRepo(db)
    repo.upsert_many([
        row(1, created="2026-10-25T10:00:00+08:00"),
        row(2, created="2026-10-25T18:00:00+08:00"),
        row(3, created="2026-10-25T14:00:00+08:00"),
    ], now_iso())
    assert [i.id for i in repo.list_by_date("2026-10-25")] == [2, 3, 1]


def test_list_by_date_unknown_date_is_empty(db):
    assert LtxRepo(db).list_by_date("1999-01-01") == []


def test_prune_removes_rows_gone_upstream(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1), row(2), row(3)], now_iso())
    assert repo.prune([1, 3]) == 1
    assert {i.id for i in repo.list_by_date("2026-10-25")} == {1, 3}


def test_prune_with_empty_keep_list_is_noop(db):
    """★ 上游临时返回空列表时不能照着 kept=[] 全删。"""
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    assert repo.prune([]) == 0
    assert repo.count() == 1


def test_mark_tg_sent_and_sent_tids(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1), row(2)], now_iso())
    repo.mark_tg_sent(1, "2026-10-26T10:00:00+08:00")
    assert repo.sent_tids([1, 2]) == {1}


def test_get_missing_returns_none(db):
    assert LtxRepo(db).get(404) is None


# ---------------------------------------------------------------- 演员（L-12）


def test_upsert_keeps_actress(db):
    """★ 演员是本地攒出来的，同步不能把它冲掉——否则每次同步都要重问一遍上游。"""
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    repo.set_actress(1, "三上悠亜", "2026-10-26T10:00:00+08:00")

    repo.upsert_many([row(1)], now_iso())

    item = repo.get(1)
    assert item.actress == "三上悠亜"
    assert item.actress_at == "2026-10-26T10:00:00+08:00"


def test_upsert_clears_actress_when_number_changes(db):
    """编号变了就不是同一部片了，旧演员必须作废。"""
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    repo.set_actress(1, "三上悠亜", "2026-10-26T10:00:00+08:00")

    repo.upsert_many([row(1, number="SSIS-999")], now_iso())

    item = repo.get(1)
    assert item.actress is None
    assert item.actress_at is None       # 重新排队回填


def test_set_actress_with_none_still_marks_time(db):
    """「确认没有演员」也要记时间——否则回填会永远重查这条。"""
    repo = LtxRepo(db)
    repo.upsert_many([row(1)], now_iso())
    repo.set_actress(1, None, "2026-10-26T10:00:00+08:00")

    item = repo.get(1)
    assert item.actress is None
    assert item.actress_at == "2026-10-26T10:00:00+08:00"
    assert repo.pending_actress(10) == []
    assert repo.actress_stats() == (1, 0)


def test_pending_actress_is_newest_first_and_limited(db):
    repo = LtxRepo(db)
    repo.upsert_many([
        row(1, date="2026-10-20"),
        row(2, date="2026-10-25"),
        row(3, date="2026-10-22"),
    ], now_iso())

    assert [i.id for i in repo.pending_actress(10)] == [2, 3, 1]
    assert [i.id for i in repo.pending_actress(2)] == [2, 3]


def test_pending_actress_skips_rejected_and_done(db):
    repo = LtxRepo(db)
    repo.upsert_many([row(1), row(2, rejected=1), row(3)], now_iso())
    repo.set_actress(3, "三上悠亜", "2026-10-26T10:00:00+08:00")

    assert [i.id for i in repo.pending_actress(10)] == [1]
    assert repo.actress_stats() == (1, 1)


def test_actress_stats_on_empty_table(db):
    """空表时 ``SUM(...)`` 全是 NULL——不能因此抛，也不能算成 1 条待办。"""
    assert LtxRepo(db).actress_stats() == (0, 0)
