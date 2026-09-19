# tests/test_cli.py
from bt169.__main__ import build_parser, main


def test_parser_has_subcommands():
    p = build_parser()
    ns = p.parse_args(["serve"])
    assert ns.command == "serve"
    assert ns.host == "127.0.0.1"          # 默认只监听回环（安全）
    assert ns.port == 8899


def test_serve_port_override():
    assert build_parser().parse_args(["serve", "--port", "9000"]).port == 9000


def test_no_command_returns_usage_error(capsys):
    assert main([]) == 2
    captured = capsys.readouterr()          # ★ 只调一次：它会清空缓冲
    assert "usage" in captured.err.lower()


def test_migrate_command(tmp_path, monkeypatch):
    """★ `__main__` 用 ``config.DB_PATH`` 动态读取（而非 `from config import DB_PATH`），
    否则 monkeypatch 不生效——后者在 import 时就把值绑死了。"""
    monkeypatch.setattr("bt169.config.DB_PATH", tmp_path / "x.db")
    assert main(["migrate"]) == 0
    assert (tmp_path / "x.db").exists()


def test_doctor_command(tmp_path, monkeypatch, capsys):
    """doctor 必须在没有数据库时也不崩——它自己会建。

    ★ 必须逐个 monkeypatch 派生的常量：``IMAGE_DIR``/``DB_PATH`` 都是
    **import 时**从 ``DATA_DIR`` 算出来的，改 ``DATA_DIR`` 不会带动它们。
    漏掉 ``IMAGE_DIR`` 会让 doctor 去查**真实项目目录**的图片，
    测试就变成了对开发机当前状态的断言。
    """
    monkeypatch.setattr("bt169.config.DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", tmp_path / "images")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "Python" in out
    assert "SQLite" in out
    assert "journal_mode=wal" in out
    assert rc == 0


def test_doctor_reports_failure_on_bad_ui_dir(tmp_path, monkeypatch, capsys):
    """UI 目录缺失时必须报 ✗ 并返回 1——否则 doctor 是个摆设。"""
    monkeypatch.setattr("bt169.config.DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr("bt169.config.UI_DIR", tmp_path / "nope")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "✗" in out
    assert rc == 1


# ------------------------------------------------------------ 图片自检（W-15）


def _seed_post_with_local_image(tmp_path, monkeypatch):
    """造一条「数据库有本地路径、磁盘上没文件」的帖子。"""
    from bt169.db import Database
    from bt169.repo.posts import PostRepo

    db_path = tmp_path / "x.db"
    monkeypatch.setattr("bt169.config.DB_PATH", db_path)
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", tmp_path / "images")

    db = Database(db_path)
    db.migrate()
    PostRepo(db).upsert_collected(
        tid=1, title="t", code=None, actress=None, release_date=None,
        size=None, cover_img="https://img.example/a.jpg", detail_img=None,
        ed2k="ed2k://|file|a|1|AB|/", post_date="2026-09-14", status="done",
        cover_local="/img/ab/ab-600.webp",     # 磁盘上并不存在
    )
    db.close()


def test_doctor_flags_missing_local_images(tmp_path, monkeypatch, capsys):
    """★ 数据库记着本地图、磁盘上却没有 → 必须报 ✗（前端会裂图）。"""
    _seed_post_with_local_image(tmp_path, monkeypatch)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "本地图丢失" in out


def test_doctor_passes_when_images_present(tmp_path, monkeypatch, capsys):
    """图片真的存在时必须 ✓ —— 否则 doctor 会永远报警，失去意义。"""
    from bt169.db import Database
    from bt169.collector.imagecache import ImageCache
    from bt169.repo.posts import PostRepo

    db_path = tmp_path / "x.db"
    image_dir = tmp_path / "images"
    monkeypatch.setattr("bt169.config.DB_PATH", db_path)
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", image_dir)

    db = Database(db_path)
    db.migrate()
    cache = ImageCache(image_dir, fetch=lambda u: _png())
    url = "https://img.example/a.jpg"
    local = cache.ensure(url).variants[600]
    PostRepo(db).upsert_collected(
        tid=1, title="t", code=None, actress=None, release_date=None,
        size=None, cover_img=url, detail_img=None,
        ed2k="ed2k://|file|a|1|AB|/", post_date="2026-09-14", status="done",
        cover_local=local,
    )
    db.close()

    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "本地图丢失" not in out
    assert "✓ 本地图片" in out


def test_doctor_flags_orphan_images(tmp_path, monkeypatch, capsys):
    """★ 没有帖子引用的图片文件必须报 ✗（删除中断遗留，白占空间）。"""
    from bt169.collector.imagecache import ImageCache
    from bt169.db import Database

    db_path = tmp_path / "x.db"
    image_dir = tmp_path / "images"
    monkeypatch.setattr("bt169.config.DB_PATH", db_path)
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", image_dir)

    db = Database(db_path)
    db.migrate()
    db.close()

    ImageCache(image_dir, fetch=lambda u: _png()).ensure("https://img.example/gone.jpg")

    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "孤儿图片" in out


def _png(w=800, h=600):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------ collect 子命令


def test_parser_has_collect():
    ns = build_parser().parse_args(
        ["collect", "--from", "2026-09-01", "--to", "2026-09-14"]
    )
    assert ns.command == "collect"
    assert ns.from_date == "2026-09-01"
    assert ns.to_date == "2026-09-14"
    assert ns.fid == "192"


def test_collect_rejects_bad_dates(capsys):
    """★ 日期非法必须返回非 0（否则 cron 无法感知失败）。"""
    assert main(["collect", "--from", "bad", "--to", "2026-09-14"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_collect_rejects_reversed_range(capsys):
    assert main(["collect", "--from", "2026-09-20", "--to", "2026-09-14"]) == 2
    assert "晚于" in capsys.readouterr().err


def test_collect_runs_and_reports(tmp_path, monkeypatch, capsys):
    """★ CLI 采集端到端（假论坛客户端，不碰网络）。

    CLI 不经过 CollectRunner（前台阻塞），所以必须单独测——
    它是 cron/定时任务要用的入口。
    """
    from bt169.collector import Collector
    from bt169.db import Database
    from bt169.repo.collect import CollectRepo
    from bt169.repo.posts import PostRepo
    from bt169.source.parse import ListRow, ThreadDetail

    monkeypatch.setattr("bt169.config.DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", tmp_path / "images")

    class FakeForum:
        def list_page(self, *, fid="192", page=1):
            if page > 1:
                return []
            return [ListRow(tid=1, title="T1", post_date="2026-09-14",
                            reply_count=0)]

        def fetch_thread(self, tid):
            return ThreadDetail(
                tid=tid, title="标题", code="ABC-1", actress="某",
                release_date="2026-09-17", size="7GB", cover_img=None,
                detail_img=None, ed2k="ed2k://|file|a.mkv|1|AB|/", locked=False,
            )

        def close(self):
            pass

    # 替换 CLI 内部构造的客户端
    import bt169.source.forum as forum_mod
    monkeypatch.setattr(forum_mod, "ForumClient", lambda **kw: FakeForum())
    import bt169.collector as collector_mod
    monkeypatch.setattr(collector_mod, "ForumClient", lambda **kw: FakeForum(),
                        raising=False)

    # CLI 是在函数内部 import 的，所以 patch 模块属性即可
    import bt169.api.routes.collect  # noqa: F401

    rc = main(["collect", "--from", "2026-09-14", "--to", "2026-09-14"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "采集 2026-09-14" in out
    assert "完成：" in out

    # ★ 必须真的落库——否则「输出对但啥也没干」也会通过
    db = Database(tmp_path / "x.db")
    try:
        row = db.read().execute("SELECT tid, ed2k FROM posts").fetchone()
    finally:
        db.close()
    assert row is not None, "CLI 采集必须真的写入数据库"
    assert row["tid"] == 1
    assert row["ed2k"]


def test_collect_progress_callback_gets_job(tmp_path, monkeypatch):
    """★ 回归：进度回调必须收到带 processed/total 的实时 job。

    早先传的是 CollectResult（跑完后的汇总），它**没有** processed
    字段 → 每帖都抛 AttributeError。因为回调是防御性的（异常只记日志），
    采集本身照跑不误，于是「进度条一帧都不动」这种故障很容易被忽略。
    """
    from bt169.collector import Collector, CollectResult
    from bt169.db import Database
    from bt169.repo.collect import CollectRepo
    from bt169.repo.posts import PostRepo
    from bt169.source.parse import ListRow, ThreadDetail

    db = Database(tmp_path / "x.db")
    db.migrate()
    seen: list[tuple[int, int]] = []

    class FakeForum:
        def list_page(self, *, fid="192", page=1):
            if page > 1:
                return []
            return [ListRow(tid=i, title=f"T{i}", post_date="2026-09-14",
                            reply_count=0) for i in (1, 2)]

        def fetch_thread(self, tid):
            return ThreadDetail(
                tid=tid, title="标题", code="ABC-1", actress=None,
                release_date=None, size=None, cover_img=None, detail_img=None,
                ed2k="ed2k://|file|a.mkv|1|AB|/", locked=False,
            )

        def close(self):
            pass

    c = Collector(client=FakeForum(), posts=PostRepo(db), jobs=CollectRepo(db))  # type: ignore[arg-type]
    c.run(from_date="2026-09-14", to_date="2026-09-14",
          on_progress=lambda j: seen.append((j.processed, j.total)))
    db.close()

    assert seen == [(1, 2), (2, 2)], f"进度回调应逐帖触发，实际 {seen}"


# ------------------------------------------------------------ doctor: 门禁


def test_doctor_reports_gate_disabled(tmp_path, monkeypatch, capsys):
    """★ 门禁状态必须显式可见——「未启用」是安全相关的信息。"""
    import bt169.config as config
    from bt169.__main__ import _cmd_doctor

    # ★ 不 patch UI_DIR：doctor 会检查它，指向不存在的目录会返回 1。
    #   沿用 test_doctor_command 的做法（只改数据目录相关常量）。
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")

    assert _cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "访问门禁" in out
    assert "未启用" in out


def test_doctor_reports_gate_enabled_and_sessions(tmp_path, monkeypatch, capsys):
    import bt169.config as config
    from bt169.api.auth import create_session, set_access_password
    from bt169.__main__ import _cmd_doctor, _load_or_create_key
    from bt169.crypto import SecretBox
    from bt169.db import Database
    from bt169.repo.settings import SettingsRepo

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")

    db = Database(config.DB_PATH)
    db.migrate()
    box = SecretBox(b"\x09" * 32)
    set_access_password(SettingsRepo(db, box), "x")
    create_session(db)
    create_session(db)
    db.close()

    # _load_or_create_key 读 config.DATA_DIR 下的密钥文件
    monkeypatch.setattr("bt169.__main__._load_or_create_key", lambda: box)

    assert _cmd_doctor() == 0
    out = capsys.readouterr().out
    assert "已启用" in out
    assert "面板会话 2" in out


def test_cleanup_sessions_removes_expired(db, box):
    """过期会话要被清掉，否则表无限增长。"""
    from datetime import datetime, timedelta

    from bt169.api.auth import cleanup_sessions, create_session

    create_session(db)
    create_session(db)
    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    with db.write() as c:
        c.execute(
            "UPDATE panel_sessions SET expires_at=? WHERE id=1", (past,)
        )
    assert cleanup_sessions(db) == 1
    left = db.read().execute("SELECT COUNT(*) FROM panel_sessions").fetchone()[0]
    assert left == 1


def _seed_post_with_dirty_size(tmp_path, monkeypatch, size="7GB@NO Watermark"):
    """塞一行带 @注解 的 size，但把库标成「已是最新」。

    这样 ``migrate()`` 不会替我们洗干净——模拟**迁移之前**就已存在的脏行。
    """
    from bt169.db import SCHEMA_VERSION, Database

    db_path = tmp_path / "dirty.db"
    monkeypatch.setattr("bt169.config.DB_PATH", db_path)
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("bt169.config.IMAGE_DIR", tmp_path / "images")

    db = Database(db_path)
    db.migrate()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO posts(tid,title,size,post_date,status,retry_count,"
            " created_at,updated_at) VALUES(1,'帖',?,'2026-09-14','done',0,'t','t')",
            (size,),
        )
        # 迁移已跑过，但数据是「历史遗留」的脏值
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.close()


def test_doctor_flags_dirty_size(tmp_path, monkeypatch, capsys):
    """★ size 里残留 @注解 → 必须报 ✗。

    解析层修好之后，**已经在库里的脏行**不会自己变干净。没有这条检查，
    卡片上会一直显示 ``7GB@NO Watermark``，而没有任何东西提醒你。
    """
    _seed_post_with_dirty_size(tmp_path, monkeypatch)
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "size 残留注解" in out


def test_doctor_passes_with_clean_size(tmp_path, monkeypatch, capsys):
    """size 干净时必须 ✓ —— 否则 doctor 永远报警，失去意义。"""
    _seed_post_with_dirty_size(tmp_path, monkeypatch, size="7GB")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "size 残留注解" not in out or "✓" in out
    assert rc == 0
