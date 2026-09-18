"""命令行入口。

设计原则：**每个子命令只做一件事**，且不依赖全局可变状态——
这样测试可以反复调用 `main()` 而互不干扰。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from bt169 import __version__, config
from bt169.crypto import SecretBox
from bt169.db import SCHEMA_VERSION, Database

__all__ = ["main", "build_parser"]

#: 主密钥文件名。放在 data/ 内，权限 0600。
KEY_FILE_NAME = "169bt.key"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bt169",
        description="169bt 归档台 —— 个人自用的 4K 帖归档与 ED2K 提取",
    )
    p.add_argument("--version", action="version", version=f"bt169 {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="启动 Web 服务")
    # 默认只监听回环：公网访问由 Lucky 反代负责（FRONTEND.md F-C9）
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8899)
    s.add_argument("--reload", action="store_true", help="开发用热重载")

    sub.add_parser("migrate", help="建库 / 升级 schema")
    sub.add_parser("doctor", help="环境自检")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    if args.command == "serve":
        return _cmd_serve(args)
    if args.command == "migrate":
        return _cmd_migrate()
    if args.command == "doctor":
        return _cmd_doctor()
    parser.print_usage(sys.stderr)
    return 2


def _cmd_migrate() -> int:
    db = Database(config.DB_PATH)
    try:
        version = db.migrate()
    finally:
        db.close()
    print(f"schema 版本：{version}（当前代码要求 {SCHEMA_VERSION}）")
    print(f"数据库：{config.DB_PATH}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from bt169.api.app import create_app

    db = Database(config.DB_PATH)
    db.migrate()
    box = _load_or_create_key()

    app = create_app(db, box=box, ui_dir=config.UI_DIR)
    print(f"→ http://{args.host}:{args.port}")
    try:
        uvicorn.run(
            app, host=args.host, port=args.port,
            reload=args.reload, log_level="info",
        )
    finally:
        db.close()
    return 0


def _cmd_doctor() -> int:
    """环境自检。所有检查项都实测，不做假设。"""
    import sqlite3
    import sys as _sys

    # (名称, 是否通过, 失败时的提示, 始终显示的备注)
    checks: list[tuple[str, bool, str, str]] = []

    v = _sys.version_info
    checks.append((
        f"Python {v.major}.{v.minor}.{v.micro}",
        v >= (3, 10),
        "需要 ≥3.10（ddddocr 约束）",
        "",
    ))

    checks.append((
        f"SQLite {sqlite3.sqlite_version}",
        sqlite3.sqlite_version_info >= (3, 35, 0),
        "需要 ≥3.35（部分索引 + UPSERT）",
        "",
    ))
    checks.append((
        f"sqlite3 threadsafety={sqlite3.threadsafety}",
        sqlite3.threadsafety == 3,
        "需要 3（串行化）才能跨线程共享连接",
        "",
    ))

    checks.append((f"数据目录 {config.DATA_DIR.name}/", True, "", str(config.DATA_DIR)))
    checks.append((
        f"前端目录 {config.UI_DIR.name}/", config.UI_DIR.is_dir(), "缺少 ui/ 目录", "",
    ))
    checks.append((
        "前端入口 index.html",
        (config.UI_DIR / "index.html").is_file(),
        "缺少 ui/index.html",
        "",
    ))

    db = Database(config.DB_PATH)
    try:
        db.migrate()
        mode = db.read().execute("PRAGMA journal_mode").fetchone()[0]
        checks.append((
            f"journal_mode={mode}", mode.lower() == "wal", "需要 WAL", "",
        ))
        n = db.read().execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        checks.append((f"帖子总数 {n}", True, "", ""))
    except Exception as exc:
        checks.append(("数据库可用", False, str(exc), ""))
    finally:
        db.close()

    key_path = config.DATA_DIR / KEY_FILE_NAME
    checks.append((
        f"主密钥 {key_path.name}",
        True,
        "",
        "尚未生成，首次 serve 时自动创建" if not key_path.exists() else "已存在",
    ))

    width = max(len(name) for name, *_ in checks)
    ok_all = True
    for name, ok, hint, note in checks:
        mark = "✓" if ok else "✗"
        line = f"  {mark} {name.ljust(width)}"
        # 失败提示只在失败时出现（「缺少 ui/ 目录」在通过时是噪音）；
        # 备注则始终显示（「首次 serve 时自动创建」在通过时才有意义）。
        detail = hint if not ok else note
        if detail:
            line += f"  ← {detail}"
        print(line)
        ok_all &= ok

    print()
    print("全部通过。" if ok_all else "存在未通过项，见上方 ✗。")
    return 0 if ok_all else 1


def _load_or_create_key() -> SecretBox:
    """读取主密钥；不存在则生成 32 字节随机密钥并写盘（0600）。

    也支持从 ``BT169_SECRET_KEY`` 注入（base64，32 字节）——
    容器化部署时避免把密钥写进镜像层。
    """
    import base64

    env = os.environ.get("BT169_SECRET_KEY")
    if env:
        try:
            return SecretBox(base64.b64decode(env, validate=True))
        except Exception as exc:
            raise SystemExit(
                f"BT169_SECRET_KEY 非法（需要 base64 编码的 32 字节）：{exc}"
            ) from exc

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DATA_DIR / KEY_FILE_NAME
    if path.exists():
        raw = base64.b64decode(path.read_text(encoding="ascii").strip())
        return SecretBox(raw)

    raw = os.urandom(32)
    path.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
    path.chmod(0o600)
    print(f"已生成主密钥：{path}（权限 0600，请勿泄露）")
    return SecretBox(raw)


if __name__ == "__main__":
    raise SystemExit(main())
