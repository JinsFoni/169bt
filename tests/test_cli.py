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
    """doctor 必须在没有数据库时也不崩——它自己会建。"""
    monkeypatch.setattr("bt169.config.DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr("bt169.config.DATA_DIR", tmp_path)
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
    monkeypatch.setattr("bt169.config.UI_DIR", tmp_path / "nope")
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "✗" in out
    assert rc == 1
