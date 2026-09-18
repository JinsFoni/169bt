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
