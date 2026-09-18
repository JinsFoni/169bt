"""SQLite 连接管理（单写 + 每线程读）与迁移执行。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bt169.config import DB_PATH

__all__ = ["Database", "SCHEMA_VERSION"]

#: 当前 schema 版本。新增迁移文件时递增。
SCHEMA_VERSION = 1

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    """SQLite 封装。

    并发模型（实测依据：``sqlite3.threadsafety == 3``）：

    - **写**：单个共享连接 + ``threading.Lock`` 串行化。实测两连接同时
      ``BEGIN IMMEDIATE`` 会得到 ``database is locked``，故必须串行。
    - **读**：每线程一个连接（WAL 下读不阻塞写）。
    """

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._local = threading.local()
        self._closed = False

        self._writer = self._connect()
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def read(self) -> sqlite3.Connection:
        """返回当前线程的读连接。"""
        conn = getattr(self._local, "reader", None)
        if conn is None:
            conn = self._connect()
            self._local.reader = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """串行化的写事务。异常时回滚。"""
        with self._lock:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except BaseException:
                self._writer.execute("ROLLBACK")
                raise
            self._writer.execute("COMMIT")

    def migrate(self) -> int:
        """应用未执行的迁移，返回最终版本号。幂等。

        ★ **不能用 ``write()`` 包 ``executescript``**：``executescript`` 会先
        隐式提交当前事务，因此在外层 ``BEGIN IMMEDIATE`` 内调用它，
        随后的 ``COMMIT`` 会报 ``cannot commit - no transaction is active``。
        迁移脚本本身应当原子——直接串行执行，靠 ``user_version`` 保证幂等。
        """
        current = self._user_version()
        for version, sql_path in self._discover():
            if version <= current:
                continue
            sql = sql_path.read_text(encoding="utf-8")
            with self._lock:
                # isolation_level=None → autocommit，无显式事务
                self._writer.executescript(sql)
                self._writer.execute(f"PRAGMA user_version={version}")
        return self._user_version()

    def _user_version(self) -> int:
        return int(self.read().execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _discover() -> list[tuple[int, Path]]:
        out: list[tuple[int, Path]] = []
        for p in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            head = p.name.split("_", 1)[0]
            if head.isdigit():
                out.append((int(head), p))
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except sqlite3.Error:
            pass
        reader = getattr(self._local, "reader", None)
        if reader is not None:
            try:
                reader.close()
            except sqlite3.Error:
                pass
