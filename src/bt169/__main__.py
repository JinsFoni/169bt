"""命令行入口。

设计原则：**每个子命令只做一件事**，且不依赖全局可变状态——
这样测试可以反复调用 `main()` 而互不干扰。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

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

    lg = sub.add_parser("login", help="登录论坛并保存会话（Cookie 30 天）")
    lg.add_argument("--username", help="论坛用户名（默认读设置里的「站点」分区）")
    lg.add_argument("--password", help="论坛密码（默认读设置）")

    tgl = sub.add_parser(
        "tg-login", help="登录 Telegram 账号（MTProto），生成 session 存入设置"
    )
    tgl.add_argument("--api-id", help="my.telegram.org 的 api_id（默认读设置 tg.api_id）")
    tgl.add_argument("--api-hash", help="my.telegram.org 的 api_hash（默认读设置 tg.api_hash）")

    c = sub.add_parser("collect", help="采集指定日期范围的帖子（前台运行）")
    c.add_argument("--from", dest="from_date", required=True,
                   metavar="YYYY-MM-DD", help="起始日期（含）")
    c.add_argument("--to", dest="to_date", required=True,
                   metavar="YYYY-MM-DD", help="结束日期（含）")
    c.add_argument("--fid", default=config.DEFAULT_FID, help="版块 ID")

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
    if args.command == "collect":
        return _cmd_collect(args)
    if args.command == "login":
        return _cmd_login(args)
    if args.command == "tg-login":
        return _cmd_tg_login(args)
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

    # ★ emby_interval=-1 启用定时同步（E-6，默认 15 分钟）。
    #   测试入口用默认的 0（不启），否则每个测试都会多一个后台线程。
    app = create_app(db, box=box, ui_dir=config.UI_DIR, emby_interval=-1)
    print(f"→ http://{args.host}:{args.port}")
    try:
        if args.reload:
            # ★ uvicorn 只认 **import 字符串**（如 ``模块:工厂``）才能启用
            #   reload/workers：传 app 对象会静默忽略 reload（仅打 WARNING）。
            #   reload 模式下 uvicorn 会 spawn 子进程重新 import 本模块，
            #   由 ``_serve_app`` 自行建库/取钥，主进程无需预先建好。
            uvicorn.run(
                "bt169.__main__:_serve_app", host=args.host, port=args.port,
                reload=True, factory=True, log_level="info",
            )
        else:
            uvicorn.run(
                app, host=args.host, port=args.port,
                log_level="info",
            )
    finally:
        db.close()
    return 0


def _serve_app():
    """reload 模式的模块级应用工厂：每个 uvicorn 子进程独立装配。"""
    import uvicorn  # noqa: F401 — 保持与 _cmd_serve 一致的导入时机

    from bt169.api.app import create_app

    db = Database(config.DB_PATH)
    db.migrate()
    box = _load_or_create_key()
    # emby_interval=-1：启用定时同步，与 _cmd_serve 一致（E-6）
    return create_app(db, box=box, ui_dir=config.UI_DIR, emby_interval=-1)


def _cmd_login(args: argparse.Namespace) -> int:
    """登录论坛并保存 Cookie。

    ★ **登录额度是稀缺资源**（5 次 / 900 秒，按 IP）。因此这里：

    - 默认复用设置里已存的账号密码，避免命令行留下密码历史
      （``--password`` 会进 shell history 与 ``ps`` 输出）
    - 只在「预校验通过的验证码」上提交登录（额度保护，见 ARCHITECTURE §5.2）
    - 额度耗尽时明确拒绝，**不重试**
    """
    from bt169.repo.settings import SettingsRepo
    from bt169.source.captcha import PythonSolver
    from bt169.source.forum import ForumClient
    from bt169.source.login import LoginClient, LoginError, QuotaExhausted
    from bt169.source.session import SessionStore

    db = Database(config.DB_PATH)
    try:
        db.migrate()
        box = _load_or_create_key()
        settings = SettingsRepo(db, box)

        username = args.username or settings.get(
            config.section_key("site", "username")
        )
        password = args.password or settings.get(
            config.section_key("site", "password")
        )
        if not username or not password:
            print(
                "缺少账号密码。请在 Web 设置面板的「站点」分区填写，"
                "或用 --username/--password 传入。",
                file=sys.stderr,
            )
            return 2

        store = SessionStore(db)
        from bt169.netproxy import httpx_kwargs

        client = ForumClient(
            db=db, client=httpx.Client(**httpx_kwargs(settings))
        )
        try:
            lc = LoginClient(
                client=client, store=store, solvers=[PythonSolver()]
            )
            print(f"登录 {username} …（验证码需解算，可能耗时几秒）")
            result = lc.login(username, password)
        except QuotaExhausted as exc:
            print(f"登录额度已耗尽：{exc}", file=sys.stderr)
            print("请等待 900 秒窗口重置后再试。", file=sys.stderr)
            return 1
        except LoginError as exc:
            print(f"登录失败：{exc}", file=sys.stderr)
            return 1
        finally:
            client.close()

        if not result.ok:
            print(f"登录失败：{result.message}", file=sys.stderr)
            if result.attempts_left is not None:
                print(f"剩余尝试次数：{result.attempts_left}", file=sys.stderr)
            return 1

        print(f"登录成功：{result.message}")
        print(f"验证码解算 {result.captcha_images} 次")
        session = store.load()
        if session is not None:
            print(f"会话有效期至：{session.expires_at}")
    finally:
        db.close()
    return 0



class _TelethonSigner:
    """Telethon 的 ``start`` 适配器：统一为同步契约。

    start(phone_cb, code_cb, password_cb) -> StringSession 字符串。
    ★ TelegramClient 构造时就要求 api_id/api_hash（Telethon 硬约束）。
    """

    def __init__(self, *, api_id: int, api_hash: str, proxy: Any = None) -> None:
        import telethon.sync  # noqa: F401  ★ 缺它协程永不执行

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(), int(api_id), api_hash, proxy=proxy
        )

    def start(self, phone_cb, code_cb, password_cb):
        self._client.start(
            phone=phone_cb, code_callback=code_cb, password=password_cb
        )
        return self._client.session.save()

    def close(self):
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _tg_signer(*, api_id: int, api_hash: str, proxy: Any = None):
    """构造登录器（独立函数便于测试替换）。"""
    return _TelethonSigner(api_id=api_id, api_hash=api_hash, proxy=proxy)


def _cmd_tg_login(args: argparse.Namespace) -> int:
    """登录 Telegram 用户账号（MTProto），StringSession 加密落库。

    ★ 交互式命令：需要手机号 → 验证码（→ 两步验证密码）。session
    等于账号凭据，因此像其他密钥一样走 SecretBox 加密（键名
    ``tg.session`` 最后一段不在 SECRET_FIELDS —— 见下方显式加密说明）。
    """
    from bt169.repo.settings import SettingsRepo

    db = Database(config.DB_PATH)
    try:
        db.migrate()
        box = _load_or_create_key()
        settings = SettingsRepo(db, box)

        api_id = (args.api_id or settings.get(
            config.section_key("tg", "api_id")) or "").strip()
        api_hash = (args.api_hash or settings.get(
            config.section_key("tg", "api_hash")) or "").strip()
        if not api_id or not api_hash:
            print(
                "缺少 api_id / api_hash。请先在 my.telegram.org 创建应用，"
                "并在 Web 设置面板的「Telegram」分区填写（或用 --api-id/--api-hash 传入）。",
                file=sys.stderr,
            )
            return 2

        print("连接 Telegram …")
        from bt169.netproxy import telethon_proxy

        signer = _tg_signer(
            api_id=int(api_id),
            api_hash=api_hash,
            proxy=telethon_proxy(settings),
        )
        try:
            # 统一契约：start(phone, code_cb, password_cb) -> session 字符串。
            # 真实实现内部包 Telethon（sync 模块已挂同步包装）。
            session_str = signer.start(
                phone_cb=lambda: input("手机号（含国家码，如 +8613800000000）：").strip(),
                code_cb=lambda: input("请输入 Telegram 发来的验证码：").strip(),
                password_cb=lambda: input("两步验证密码（未设置请直接回车）：").strip(),
            )
        except Exception as exc:  # noqa: BLE001 — 登录层什么都可能抛
            print(f"登录失败：{exc}", file=sys.stderr)
            return 1
        finally:
            close = getattr(signer, "close", None)
            if close:
                close()

        if not session_str:
            print("登录失败：未取得会话", file=sys.stderr)
            return 1

        # StringSession 是账号凭据 → 必须加密。键名 ``tg.session`` 最后一段
        # 是 ``session``，不在 SECRET_FIELDS 中，这里显式走 box.encrypt。
        key = config.section_key("tg", "session")
        with db.write() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, encrypted, updated_at)"
                " VALUES(?,?,1,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, encrypted=1, updated_at=excluded.updated_at",
                (key, box.encrypt(session_str), __import__("bt169.repo.posts", fromlist=["now_iso"]).now_iso()),
            )
        print("登录成功：session 已加密存入设置（tg.session）。")
        print("如尚未填写，请在设置页补全 tg.api_id / tg.api_hash / tg.target。")
    finally:
        db.close()
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    """命令行采集（**前台**跑，实时打印进度）。

    与 ``POST /api/collect`` 的区别：这里同步阻塞，适合定时任务
    （cron / launchd / 容器 CMD）和首次历史回填——那些场景不需要
    进度轮询，只需要「跑完并告诉我结果」。

    ★ 不经过 ``CollectRunner``：runner 的意义是「HTTP 立即返回 +
    后台线程」，而 CLI 本来就该等。少一层线程也少一处竞态。
    """
    from bt169.collector import (
        CollectError,
        Collector,
        ValidateError,
        date_range,
    )
    from bt169.collector.imagecache import ImageCache
    from bt169.repo.collect import CollectRepo
    from bt169.repo.posts import PostRepo
    from bt169.repo.settings import SettingsRepo
    from bt169.source.forum import ForumClient
    from bt169.source.session import SessionStore
    from bt169.source.thanks import ThanksClient

    try:
        days = date_range(args.from_date, args.to_date)
    except ValidateError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    db = Database(config.DB_PATH)
    try:
        db.migrate()
        box = _load_or_create_key()
        settings = SettingsRepo(db, box)

        # 复用已保存的会话（与 API 路径一致，见 api/routes/collect.py）
        session = SessionStore(db).load()
        cookies = session.cookies if session and session.valid else None
        if not cookies:
            print("提示：无有效登录会话 → 帖子会停在 pending（无法感谢解锁）",
                  file=sys.stderr)

        client = ForumClient(cookies=cookies, db=db)
        collector = Collector(
            client=client,
            posts=PostRepo(db),
            jobs=CollectRepo(db),
            thanks=ThanksClient(client=client) if cookies else None,
            images=ImageCache(config.IMAGE_DIR),
        )
        print(f"采集 {days[0]} ~ {days[-1]}（{len(days)} 天，fid={args.fid}）")
        result = collector.run(
            from_date=args.from_date, to_date=args.to_date, fid=args.fid,
            on_progress=lambda j: print(
                f"  [{j.processed}/{j.total}] 新增 {j.collected}"
                f" 跳过 {j.skipped} 失败 {j.failed}",
                flush=True,
            ),
        )
    except CollectError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"完成：新增 {result.collected}，跳过 {result.skipped}，"
          f"失败 {result.failed}，共 {result.total} 帖")
    return 0


def _image_checks(db) -> list[tuple[str, bool, str, str]]:  # type: ignore[no-untyped-def]
    """图片本地化自检（W-15）。

    检查两类真实故障：

    1. **数据库有本地路径，磁盘上却没有文件** → 前端裂图。
       典型成因：手动删了 ``data/images/``、换机迁移只带走了 db、
       或某次下载失败。采集时会对「已跳过」的帖子顺带补图
       （见 ``Collector._repair_images``），所以重跑同区间即可自愈。
    2. **孤儿文件**（没有任何帖子引用）→ 白占空间。
       典型成因：删除帖子时进程被杀，只删了行没删文件。
    """
    from bt169.collector.imagecache import ImageCache
    from bt169.repo.posts import PostRepo

    out: list[tuple[str, bool, str, str]] = []
    cache = ImageCache(config.IMAGE_DIR)
    posts = PostRepo(db)

    # ---- 1. 数据库引用的本地图是否真的存在 ----
    missing: list[int] = []
    for row in db.read().execute(
        "SELECT tid, cover_local, detail_local FROM posts"
    ):
        for local in (row["cover_local"], row["detail_local"]):
            if not local:
                continue
            rel = str(local).removeprefix("/img/")
            if not (config.IMAGE_DIR / rel).is_file():
                missing.append(row["tid"])
                break

    size_mb = cache.total_bytes() / 1024 / 1024
    out.append((
        f"本地图片 {size_mb:.1f} MB",
        not missing,
        f"{len(missing)} 个帖子的本地图丢失（如 {missing[:3]}）"
        "—— 跑一次同区间的采集即可自动补回（无需清库）",
        "",
    ))

    # ---- 2. 孤儿文件 ----
    known = {
        ImageCache.key_for(url)
        for row in db.read().execute(
            "SELECT cover_img, detail_img FROM posts"
        )
        for url in (row["cover_img"], row["detail_img"])
        if url
    }
    orphans = cache.orphans(known)
    orphan_mb = sum(p.stat().st_size for p in orphans) / 1024 / 1024
    out.append((
        f"孤儿图片 {len(orphans)} 个",
        not orphans,
        f"占用 {orphan_mb:.1f} MB，无帖子引用（删除中断遗留）",
        "",
    ))
    return out


def _size_checks(db) -> list[tuple[str, bool, str, str]]:  # type: ignore[no-untyped-def]
    """检查 ``size`` 里是否残留 ``@注解``。

    站点把「值 + 补充说明」写成一行（``7GB@NO Watermark``）。解析层
    已在 ``_strip_annotation()`` 里修好，但**迁移之前就已存在的脏行**
    只能靠 ``005_fix_size.sql`` 洗一次。

    没有这条检查，脏值会一直显示在卡片上而无人察觉——修了解析层
    不等于修了历史数据。
    """
    bad = [
        row[0]
        for row in db.read().execute(
            "SELECT tid FROM posts WHERE size LIKE '%@%' ORDER BY tid LIMIT 3"
        )
    ]
    n = db.read().execute(
        "SELECT COUNT(*) FROM posts WHERE size LIKE '%@%'"
    ).fetchone()[0]
    return [(
        f"size 残留注解 {n} 行",
        n == 0,
        f"如 {bad} —— 跑一次 bt169 migrate 即可洗净",
        "",
    )]


def _emby_checks(db) -> list[tuple[str, bool, str, str]]:  # type: ignore[no-untyped-def]
    """Emby 入库标记状态（E-1~E-7）。

    ★ **不发网络请求**：doctor 不该因为 Emby 没开机而变慢或报错。
    这里只检查本地状态（是否配置、标记是否已同步过）。
    """
    from bt169 import config

    checks: list[tuple[str, bool, str, str]] = []

    row = db.read().execute(
        "SELECT COUNT(*) AS n FROM settings WHERE key IN (?,?)",
        (config.section_key("emby", "url"),
         config.section_key("emby", "api_key")),
    ).fetchone()
    configured = (row["n"] or 0) == 2

    checks.append((
        "Emby 配置",
        True,
        "已配置" if configured else "未配置",
        "" if configured else "未配置时不显示「已入库」标记，浏览不受影响（E-7）",
    ))

    stats = db.read().execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN emby_status='in_library' THEN 1 ELSE 0 END) AS lib,"
        " SUM(CASE WHEN emby_checked IS NOT NULL THEN 1 ELSE 0 END) AS checked"
        " FROM posts"
    ).fetchone()

    total = stats["total"] or 0
    checked = stats["checked"] or 0
    lib = stats["lib"] or 0

    if total == 0:
        detail = "暂无帖子"
    elif checked == 0:
        detail = f"{total} 帖尚未核对"
    else:
        detail = f"已核对 {checked}/{total} 帖，命中 {lib} 帖"

    checks.append((
        "Emby 入库标记",
        True,
        detail,
        "" if checked else "打开设置页点「刷新入库状态」即可同步",
    ))
    return checks


def _auth_checks(db) -> list[tuple[str, bool, str, str]]:  # type: ignore[no-untyped-def]
    """访问门禁与面板会话状态（S-6）。"""
    from bt169.api.auth import cleanup_sessions

    checks: list[tuple[str, bool, str, str]] = []

    row = db.read().execute(
        "SELECT value FROM settings WHERE key='basic.password'"
    ).fetchone()
    gate_on = row is not None and bool(row["value"])
    checks.append((
        "访问门禁",
        True,                      # 开与关都是合法状态
        "",
        "已启用（API 与 /img 需要密码）" if gate_on else "未启用（任何人可访问）",
    ))

    if gate_on:
        sessions = db.read().execute(
            "SELECT COUNT(*) FROM panel_sessions"
        ).fetchone()[0]
        checks.append((
            f"面板会话 {sessions}", True, "",
            "无已登录设备" if sessions == 0 else "有效设备数（含手机）",
        ))
        expired = cleanup_sessions(db)
        if expired:
            checks.append((
                f"清理过期会话 {expired}", True, "", "已删除",
            ))

    return checks


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
        checks.extend(_image_checks(db))
        checks.extend(_size_checks(db))
        checks.extend(_auth_checks(db))
        checks.extend(_emby_checks(db))
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
    """读取主密钥（实现已下沉到 :func:`bt169.crypto.load_secret_box`）。"""
    from bt169.crypto import load_secret_box

    return load_secret_box()


if __name__ == "__main__":
    raise SystemExit(main())
