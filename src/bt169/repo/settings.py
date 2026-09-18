"""KV 配置读写。密钥字段加密落库、脱敏读取。"""

from __future__ import annotations

from bt169.config import PLACEHOLDER, is_secret
from bt169.crypto import SecretBox
from bt169.db import Database
from bt169.repo.posts import now_iso

__all__ = ["SettingsRepo"]


class SettingsRepo:
    """``settings`` 表的读写封装。

    - 写入时，键名最后一段在 :data:`SECRET_FIELDS` 中的值自动 AES-256-GCM
      加密，并置 ``encrypted=1``。
    - 读取时透明解密；``get_all(masked=True)`` 把密钥字段替换为
      :data:`PLACEHOLDER`，供 API 返回给前端。
    - ``put_many`` 跳过等于占位符的值——前端回传未修改的密钥字段时
      不应把占位符写进数据库覆盖真实密钥。
    - **键名必须带分区前缀**（``site.password``，见 :func:`section_key`）。
    """

    def __init__(self, db: Database, box: SecretBox) -> None:
        self._db = db
        self._box = box

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.read().execute(
            "SELECT value, encrypted FROM settings WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        value = row["value"]
        if row["encrypted"]:
            return self._box.decrypt(value) if value is not None else None
        return value

    def get_all(self, *, masked: bool = False) -> dict[str, str]:
        """全部设置。

        Args:
            masked: 为 True 时密钥字段返回 :data:`PLACEHOLDER`。
        """
        rows = self._db.read().execute(
            "SELECT key, value, encrypted FROM settings"
        ).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            key, value = r["key"], r["value"]
            if masked and is_secret(key):
                out[key] = PLACEHOLDER
            elif r["encrypted"] and value is not None:
                out[key] = self._box.decrypt(value)
            else:
                out[key] = value
        return out

    def put(self, key: str, value: str) -> None:
        secret = is_secret(key)
        stored = self._box.encrypt(value) if secret else value
        with self._db.write() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, encrypted, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, encrypted=excluded.encrypted,"
                " updated_at=excluded.updated_at",
                (key, stored, 1 if secret else 0, now_iso()),
            )

    def put_many(self, items: dict[str, str]) -> list[str]:
        """批量写入，跳过占位符。

        Returns:
            被跳过的键名（即前端未修改的密钥字段）。
        """
        skipped: list[str] = []
        for key, value in items.items():
            if is_secret(key) and value == PLACEHOLDER:
                skipped.append(key)
                continue
            self.put(key, value)
        return skipped

    def delete(self, key: str) -> None:
        with self._db.write() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
