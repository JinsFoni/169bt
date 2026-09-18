# 后端地基与只读 API 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `169bt serve` 起得来、`169bt doctor` 能自检、只读 API 能被现有前端消费——为后续采集/登录/集成打好地基。

**Architecture:** 单进程 Python 应用。SQLite（内置 `sqlite3`，WAL）作为唯一存储，采用「单写连接 + 线程锁，读连接每线程一个」的并发模型（实测 `threadsafety==3`）。FastAPI 提供 `/api/*` 并由 `StaticFiles` 分发 `ui/`，前后端同源（Lucky 反代）。本计划**不实现采集与登录**——数据库可手工灌入数据。

**Tech Stack:** Python 3.14（要求 ≥3.10）· FastAPI · uvicorn · 内置 `sqlite3` · `cryptography`（AES-256-GCM）· pytest

**Spec:** `ARCHITECTURE.md`（§3 目录、§4 数据层、§5.6 API、§7 契约、§9 安全）、`FRONTEND.md`（§3.1 API 契约、§14.1 `/api/status`）

## Global Constraints

- Python **≥3.10**（`ddddocr` 约束）；本机实测 3.14.6
- **不使用 ORM**，用内置 `sqlite3` + 手写 SQL
- **不使用 aiosqlite**（实测 `threadsafety==3`，同步足够；见 ADR-15）
- SQLite 必须开 `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000`
- **写操作必须串行**（实测：两连接同时 `BEGIN IMMEDIATE` → `database is locked`）
- **硬删除**：`posts` 表**没有** `deleted_at` 列（用户决策，见 ADR-7）
- API 字段名对齐前端：`cover` / `detail`（**不是** `cover_img` / `detail_img`）——见 ADR-11
- 静态分发目录为 `ui/`，`ui/data.js` 在本计划中**保持不动**（F0 之后才删）
- 密钥字段（`password` / `token` / `apikey`）落库前必须 AES-256-GCM 加密，`settings.encrypted=1`
- 读取设置时密钥字段返回**脱敏占位符** `••••••••`（8 个 `•`），提交时等于占位符视为「未修改」
- 时间戳统一 ISO 8601 字符串，归档日期 `post_date` 用 **UTC+8**（C-9）
- 提交信息用 Conventional Commits（`feat:` / `test:` / `chore:`）

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 项目元数据 + 依赖 + pytest 配置 |
| `src/bt169/__init__.py` | 版本号 |
| `src/bt169/config.py` | 路径、常量、RSS URL 解析、字段分类 |
| `src/bt169/crypto.py` | AES-256-GCM 加解密 + 口令哈希 |
| `src/bt169/db.py` | 连接管理（单写 + 线程读）、迁移执行 |
| `src/bt169/migrations/001_init.sql` | 初始 schema |
| `src/bt169/models.py` | `Post` 领域对象 + `PostDTO`（字段名映射） |
| `src/bt169/repo/posts.py` | 帖子查询、硬删除、状态变更 |
| `src/bt169/repo/archive.py` | 归档日期列表、相邻日期导航 |
| `src/bt169/repo/settings.py` | KV 配置（含加密字段与脱敏） |
| `src/bt169/api/app.py` | FastAPI 装配 |
| `src/bt169/api/deps.py` | 依赖注入（db / settings） |
| `src/bt169/api/static.py` | `ui/` 静态分发 |
| `src/bt169/api/routes/posts.py` | `/api/dates`、`/api/posts`、`DELETE /api/posts/{tid}` |
| `src/bt169/api/routes/settings.py` | `/api/settings` |
| `src/bt169/api/routes/health.py` | `/api/health` |
| `src/bt169/__main__.py` | CLI 子命令（`serve` / `migrate` / `doctor`） |
| `tests/conftest.py` | pytest fixtures（临时 DB、TestClient） |
| `tests/test_config.py` | RSS 解析、字段分类 |
| `tests/test_crypto.py` | 加解密往返、篡改检测 |
| `tests/test_db.py` | 迁移、WAL、并发写串行化 |
| `tests/test_models.py` | DTO 映射、字段不外泄 |
| `tests/test_repo_posts.py` | 查询、硬删除、状态机 |
| `tests/test_repo_archive.py` | 日期列表、相邻导航（含空档） |
| `tests/test_repo_settings.py` | 加密落库、脱敏读取、占位符语义 |
| `tests/test_api.py` | 契约字段名、DTO 映射、删除 |

---

## Task 1: 项目骨架与 git 初始化

**Files:**
- Create: `pyproject.toml`
- Create: `src/bt169/__init__.py`
- Create: `.gitignore`
- Create: `tests/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: 可 `pip install -e .` 的包 `bt169`；`bt169.__version__`

- [ ] **Step 1: 初始化 git 仓库**

当前目录**不是** git 仓库（已实测确认）。先初始化：

```bash
cd /Users/mario/Dev/Projects/169bt
git init
```

- [ ] **Step 2: 写 `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.venv/
venv/
*.egg-info/
.pytest_cache/
.coverage
htmlcov/
data/
*.db
*.db-wal
*.db-shm
.DS_Store
.env
```

> `data/` 与 `*.db` 必须忽略：数据库含加密后的密钥与论坛 Cookie。

- [ ] **Step 3: 写 `pyproject.toml`**

```toml
[project]
name = "bt169"
version = "0.1.0"
description = "169bt 归档台 —— 个人自用的 4K 帖归档与 ED2K 提取"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "cryptography>=42",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
bt169 = "bt169.__main__:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
bt169 = ["migrations/*.sql"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-q"
```

- [ ] **Step 4: 写 `src/bt169/__init__.py`**

```python
"""169bt 归档台 —— 后端。"""

__version__ = "0.1.0"
```

- [ ] **Step 5: 建 `tests/__init__.py`**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 6: 建 venv 并安装**

```bash
cd /Users/mario/Dev/Projects/169bt
python3 -m venv .venv
.venv/bin/pip install -q -e ".[dev]"
.venv/bin/python -c "import bt169; print(bt169.__version__)"
```

Expected: 输出 `0.1.0`

> ★ **`/tmp` 不适合放 venv**（Spotlight 会索引）。项目内 `.venv/` 已在 `.gitignore`。
> 若仍触发 Spotlight，执行 `touch .venv/.metadata_never_index`。

- [ ] **Step 7: 提交**

```bash
git add .gitignore pyproject.toml src/bt169/__init__.py tests/__init__.py
git commit -m "chore: 项目骨架与依赖声明"
```

---

## Task 2: `config.py` —— 路径、常量、RSS 解析

**Files:**
- Create: `src/bt169/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `PROJECT_ROOT: Path`、`DATA_DIR: Path`、`DB_PATH: Path`、`IMAGE_DIR: Path`、`UI_DIR: Path`
  - `SECRET_FIELDS: frozenset[str]` —— 需加密的设置键名
  - `SETTINGS_SECTIONS: tuple[str, ...]` —— `("site","basic","proxy","emby","tg")`
  - `PLACEHOLDER: str` —— `••••••••`
  - `parse_rss_url(raw: str) -> tuple[str, str]` —— 返回 `(fid, clean_url)`
  - `class ConfigError(ValueError)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
import pytest
from bt169.config import (
    ConfigError, PLACEHOLDER, SECRET_FIELDS, SETTINGS_SECTIONS, is_secret,
    parse_rss_url, section_key,
)


def test_parse_rss_url_extracts_fid():
    fid, clean = parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=192")
    assert fid == "192"
    assert clean == "https://169bt.com/forum.php?fid=192"


def test_parse_rss_url_strips_auth():
    """实测：RSS 匿名可用，auth 被服务端忽略 → 不应留在配置里。"""
    fid, clean = parse_rss_url(
        "https://169bt.com/forum.php?mod=rss&fid=192&auth=deadbeef&ver=2"
    )
    assert fid == "192"
    assert "auth" not in clean
    assert "ver" not in clean
    assert clean == "https://169bt.com/forum.php?fid=192"


def test_parse_rss_url_rejects_missing_fid():
    with pytest.raises(ConfigError, match="fid"):
        parse_rss_url("https://169bt.com/forum.php?mod=rss")


def test_parse_rss_url_rejects_non_numeric_fid():
    with pytest.raises(ConfigError, match="数字"):
        parse_rss_url("https://169bt.com/forum.php?mod=rss&fid=abc")


def test_parse_rss_url_rejects_empty():
    with pytest.raises(ConfigError):
        parse_rss_url("")


def test_parse_rss_url_rejects_relative():
    with pytest.raises(ConfigError, match="http"):
        parse_rss_url("/forum.php?mod=rss&fid=192")


def test_secret_fields_cover_credentials():
    assert {"password", "token", "apikey"} <= SECRET_FIELDS


def test_is_secret_handles_namespaced_keys():
    """★ 设置是扁平 KV 表，`username`/`password` 在「站点」与「代理」两个
    分区都出现 → 必须用 `section.key` 命名空间避免互相覆盖。"""
    assert is_secret("site.password")
    assert is_secret("proxy.password")
    assert is_secret("tg.token")
    assert not is_secret("site.username")
    assert not is_secret("basic.panel_password")   # 不是 password 后缀


def test_section_key_namespacing():
    assert section_key("site", "password") == "site.password"
    assert section_key("proxy", "password") == "proxy.password"
    # 两个分区的同名字段必须是不同的存储键
    assert section_key("site", "password") != section_key("proxy", "password")


def test_settings_sections_site_first():
    """用户决策：新增「站点」分区（RSS + 账号密码）并置于首位。"""
    assert SETTINGS_SECTIONS[0] == "site"
    assert set(SETTINGS_SECTIONS) == {"site", "basic", "proxy", "emby", "tg"}


def test_placeholder_is_eight_dots():
    assert PLACEHOLDER == "\u2022" * 8
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.config'`

- [ ] **Step 3: 实现 `config.py`**

```python
"""路径、常量与配置解析。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

__all__ = [
    "ConfigError", "PROJECT_ROOT", "DATA_DIR", "DB_PATH", "IMAGE_DIR", "UI_DIR",
    "SECRET_FIELDS", "SETTINGS_SECTIONS", "PLACEHOLDER", "parse_rss_url",
    "section_key", "is_secret",
]

# 项目根：src/bt169/config.py → src/bt169 → src → 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "169bt.db"
IMAGE_DIR = DATA_DIR / "images"
UI_DIR = PROJECT_ROOT / "ui"

#: 落库前必须加密的设置键名。
SECRET_FIELDS = frozenset({"password", "token", "apikey", "api_key", "secret"})

#: 设置分区（与前端设置弹窗一一对应）。顺序即展示顺序。
SETTINGS_SECTIONS = ("site", "basic", "proxy", "emby", "tg")

#: 密钥字段读取时返回的占位符。提交时等于此值 → 视为「未修改」。
PLACEHOLDER = "\u2022" * 8  # ••••••••


class ConfigError(ValueError):
    """配置非法。"""


def section_key(section: str, key: str) -> str:
    """把分区内字段名转成存储键。

    ★ **必须有命名空间**：``settings`` 是扁平 KV 表，而 ``username`` /
    ``password`` 在「站点」与「网络代理」两个分区**都存在**。
    若直接存裸字段名，后写入的分区会静默覆盖先写入的——用户会发现
    「改了代理密码，论坛密码也没了」。
    """
    return f"{section}.{key}"


def is_secret(key: str) -> bool:
    """字段是否需加密。

    接受存储键（``site.password``）或裸字段名（``password``）——
    只比较**最后一段**，因此 ``panel_password`` 不会被误判为密钥字段。
    """
    return key.rsplit(".", 1)[-1] in SECRET_FIELDS


def parse_rss_url(raw: str) -> tuple[str, str]:
    """从 RSS 订阅链接解析出版块 fid，并返回清洗后的 URL。

    实测（REQUIREMENTS.md 事实 #21）：RSS 匿名完全可用，``auth`` 参数被服务端忽略。
    因此清洗时**只保留 fid**——不必要地在 URL 里携带账号凭据是安全风险。

    Args:
        raw: 用户填写的完整 RSS URL。

    Returns:
        ``(fid, clean_url)``。

    Raises:
        ConfigError: URL 为空、非法，或缺 ``fid`` 参数。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("RSS 订阅链接不能为空")

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"RSS 订阅链接必须是完整的 http(s) URL：{raw!r}")

    fid = (parse_qs(parsed.query).get("fid") or [""])[0].strip()
    if not fid:
        raise ConfigError("RSS 订阅链接缺少 fid 参数（版块 ID）")
    if not fid.isdigit():
        raise ConfigError(f"fid 必须是数字：{fid!r}")

    clean = urlunparse(parsed._replace(query=f"fid={fid}", fragment=""))
    return fid, clean
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/bt169/config.py tests/test_config.py
git commit -m "feat(config): 路径常量、密钥字段分类与 RSS URL 解析"
```

---

## Task 3: `crypto.py` —— AES-256-GCM

**Files:**
- Create: `src/bt169/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class SecretBox`，构造 `SecretBox(key: bytes)`，要求 `len(key)==32`
  - `SecretBox.from_passphrase(passphrase: str, *, salt: bytes) -> SecretBox`
  - `SecretBox.encrypt(plaintext: str) -> str` —— 返回 `base64(nonce||ciphertext)`
  - `SecretBox.decrypt(token: str) -> str`
  - `class DecryptError(Exception)`
  - `hash_password(pw: str) -> str`、`verify_password(pw: str, stored: str) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_crypto.py
import base64

import pytest

from bt169.crypto import DecryptError, SecretBox, hash_password, verify_password

KEY = b"\x01" * 32


def test_roundtrip():
    box = SecretBox(KEY)
    assert box.decrypt(box.encrypt("hunter2")) == "hunter2"


def test_roundtrip_unicode():
    box = SecretBox(KEY)
    pw = "密码-p@ssw0rd-\U0001f510"
    assert box.decrypt(box.encrypt(pw)) == pw


def test_ciphertext_differs_each_time():
    """GCM 随机 nonce → 同一明文两次密文不同。"""
    box = SecretBox(KEY)
    assert box.encrypt("same") != box.encrypt("same")


def test_tamper_detected():
    """GCM 认证标签必须能发现篡改。"""
    box = SecretBox(KEY)
    tok = bytearray(base64.b64decode(box.encrypt("secret")))
    tok[-1] ^= 0x01                      # 翻转密文最后一字节
    bad = base64.b64encode(bytes(tok)).decode()
    with pytest.raises(DecryptError):
        box.decrypt(bad)


def test_wrong_key_fails():
    tok = SecretBox(KEY).encrypt("secret")
    with pytest.raises(DecryptError):
        SecretBox(b"\x02" * 32).decrypt(tok)


def test_key_length_enforced():
    with pytest.raises(ValueError, match="32"):
        SecretBox(b"tooshort")


def test_from_passphrase_is_deterministic():
    """同一口令 + 同一 salt 必须得到同一密钥，否则重启后解不开配置。"""
    a = SecretBox.from_passphrase("pw", salt=b"s" * 16)
    b = SecretBox.from_passphrase("pw", salt=b"s" * 16)
    assert a.decrypt(b.encrypt("x")) == "x"


def test_from_passphrase_different_salt_differs():
    a = SecretBox.from_passphrase("pw", salt=b"s" * 16)
    b = SecretBox.from_passphrase("pw", salt=b"t" * 16)
    with pytest.raises(DecryptError):
        b.decrypt(a.encrypt("x"))


def test_decrypt_rejects_garbage():
    with pytest.raises(DecryptError):
        SecretBox(KEY).decrypt("not base64 !!!")


def test_decrypt_rejects_too_short():
    with pytest.raises(DecryptError, match="长度"):
        SecretBox(KEY).decrypt(base64.b64encode(b"short").decode())


def test_password_hash_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)


def test_password_hash_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_malformed():
    assert not verify_password("x", "garbage")
    assert not verify_password("x", "")
    assert not verify_password("x", "md5$1$a$b")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.crypto'`

- [ ] **Step 3: 实现 `crypto.py`**

```python
"""对称加密与口令哈希。"""

from __future__ import annotations

import base64
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

__all__ = ["SecretBox", "DecryptError", "hash_password", "verify_password"]

_NONCE_BYTES = 12          # GCM 推荐 96 bit
_KDF_ITERATIONS = 600_000  # OWASP 对 PBKDF2-SHA256 的建议量级


class DecryptError(Exception):
    """密文无法解密（密钥错误、被篡改或格式非法）。"""


class SecretBox:
    """AES-256-GCM 加解密。

    密文格式：``base64(nonce || ciphertext || tag)``。
    GCM 自带完整性校验，因此无需额外 HMAC。
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"AES-256 需要 32 字节密钥，收到 {len(key)} 字节")
        self._aes = AESGCM(key)

    @classmethod
    def from_passphrase(cls, passphrase: str, *, salt: bytes) -> SecretBox:
        """由口令派生密钥（PBKDF2-SHA256）。

        用于「用户未提供随机密钥」时从访问密码派生——同一口令 + 同一 salt
        必须得到同一密钥，否则重启后无法解密已存配置。
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_KDF_ITERATIONS,
        )
        return cls(kdf.derive(passphrase.encode("utf-8")))

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        blob = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + blob).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            raw = base64.b64decode(token, validate=True)
        except Exception as exc:  # noqa: BLE001 - 归一化为 DecryptError
            raise DecryptError("密文不是合法的 base64") from exc

        if len(raw) <= _NONCE_BYTES:
            raise DecryptError("密文长度不足")

        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return self._aes.decrypt(nonce, blob, None).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise DecryptError("解密失败：密钥错误或密文被篡改") from exc


def hash_password(password: str, *, iterations: int = _KDF_ITERATIONS) -> str:
    """PBKDF2-SHA256 哈希，格式 ``pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>``。"""
    salt = os.urandom(16)
    digest = _pbkdf2(password, salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """常数时间校验口令。格式非法一律返回 False（不抛异常）。"""
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = _pbkdf2(password, salt, int(iters_s))
    except Exception:  # noqa: BLE001 - 格式非法即校验失败
        return False
    return hmac.compare_digest(actual, expected)


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return kdf.derive(password.encode("utf-8"))
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: 13 passed

- [ ] **Step 5: 提交**

```bash
git add src/bt169/crypto.py tests/test_crypto.py
git commit -m "feat(crypto): AES-256-GCM 加密与 PBKDF2 口令哈希"
```

---

## Task 4: `db.py` + 初始迁移

**Files:**
- Create: `src/bt169/migrations/001_init.sql`
- Create: `src/bt169/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `bt169.config.DB_PATH`
- Produces:
  - `class Database`，构造 `Database(path: Path | str)`
  - `Database.write()` —— 上下文管理器，持锁，yield `sqlite3.Connection`
  - `Database.read() -> sqlite3.Connection` —— 当前线程的读连接
  - `Database.migrate() -> int` —— 应用未执行的迁移，返回最终版本号
  - `Database.close() -> None`
  - `SCHEMA_VERSION: int`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_db.py
import sqlite3
import threading

import pytest

from bt169.db import SCHEMA_VERSION, Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


def _insert(conn, key):
    conn.execute(
        "INSERT INTO settings(key,value,encrypted,updated_at)"
        " VALUES(?,?,0,'2026-01-01T00:00:00+08:00')",
        (key, "v"),
    )


def test_migrate_creates_tables(db):
    names = {
        r[0] for r in db.read().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"posts", "settings", "forum_session", "panel_session"} <= names


def test_migrate_is_idempotent(db):
    assert db.migrate() == SCHEMA_VERSION


def test_wal_enabled(db):
    assert db.read().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_set(db):
    assert db.read().execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_posts_has_no_deleted_at(db):
    """硬删除（ADR-7）：schema 不应有软删除列。"""
    cols = {r[1] for r in db.read().execute("PRAGMA table_info(posts)")}
    assert "deleted_at" not in cols
    assert {"tid", "code", "ed2k", "post_date", "status"} <= cols


def test_posts_status_is_not_null(db):
    """status 由应用层校验；SQLite 无枚举，靠 VALID_STATUSES。
    ``PRAGMA table_info`` 的列：cid, name, type, notnull, dflt_value, pk。
    """
    info = {r[1]: r for r in db.read().execute("PRAGMA table_info(posts)")}
    assert info["status"][3] == 1          # notnull


def test_migrate_does_not_break_transactions(db):
    """★ 回归：`executescript` 会隐式提交，迁移后 `write()` 必须仍可用。"""
    with db.write() as conn:
        _insert(conn, "after-migrate")
    assert db.read().execute(
        "SELECT COUNT(*) FROM settings WHERE key='after-migrate'"
    ).fetchone()[0] == 1


def test_write_context_commits(db):
    with db.write() as conn:
        _insert(conn, "k")
    row = db.read().execute("SELECT value FROM settings WHERE key='k'").fetchone()
    assert row[0] == "v"


def test_write_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            _insert(conn, "x")
            raise RuntimeError("boom")
    n = db.read().execute("SELECT COUNT(*) FROM settings WHERE key='x'").fetchone()[0]
    assert n == 0


def test_concurrent_writes_are_serialized(db):
    """实测：两连接同时 BEGIN IMMEDIATE → 'database is locked'。
    单写连接 + 锁必须让并发写全部成功。"""
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            with db.write() as conn:
                _insert(conn, f"k{n}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    n = db.read().execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    assert n == 20


def test_read_connection_is_per_thread(db):
    main = db.read()
    other: list[sqlite3.Connection] = []
    t = threading.Thread(target=lambda: other.append(db.read()))
    t.start()
    t.join()
    assert other[0] is not main


def test_reader_returns_same_conn_within_thread(db):
    assert db.read() is db.read()


def test_close_is_idempotent(db):
    db.close()
    db.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.db'`

- [ ] **Step 3: 写 `src/bt169/migrations/001_init.sql`**

```sql
-- 001_init.sql —— 初始 schema
-- 注意：硬删除（ADR-7），posts 表无 deleted_at 列。

CREATE TABLE posts (
  tid           INTEGER PRIMARY KEY,       -- 论坛帖子 ID
  title         TEXT    NOT NULL DEFAULT '',
  code          TEXT,                      -- 番号，如 START-624
  actress       TEXT,
  release_date  TEXT,                      -- 商品発売日 YYYY-MM-DD
  size          TEXT,                      -- 如 7GB
  cover_img     TEXT,                      -- 封面图源 URL（API 中映射为 cover）
  detail_img    TEXT,                      -- 详情图源 URL（API 中映射为 detail）
  ed2k          TEXT,                      -- NULL = 未解锁或无链接
  post_date     TEXT    NOT NULL,          -- 归档依据 YYYY-MM-DD (UTC+8)
  post_time     TEXT,                      -- 完整时间戳 RFC3339 (UTC+8)
  status        TEXT    NOT NULL,          -- pending|thanked|done|failed|nolink
  retry_count   INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  next_retry_at TEXT,
  emby_status   TEXT,                      -- none|in_library（缓存）
  emby_item_id  TEXT,
  emby_checked  TEXT,
  tg_sent_at    TEXT,                      -- NULL = 未转发
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL
);

CREATE INDEX idx_browse  ON posts(post_date)     WHERE status='done';
CREATE INDEX idx_pending ON posts(next_retry_at) WHERE status IN ('pending','failed','thanked');
CREATE INDEX idx_code    ON posts(code)          WHERE code IS NOT NULL;
CREATE INDEX idx_emby    ON posts(emby_checked);
CREATE INDEX idx_tg      ON posts(tg_sent_at)    WHERE tg_sent_at IS NULL;

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  encrypted  INTEGER NOT NULL DEFAULT 0,   -- 1 = value 为密文
  updated_at TEXT NOT NULL
);

CREATE TABLE forum_session (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  cookies             TEXT    NOT NULL,    -- JSON 序列化 cookie 列表
  username            TEXT,
  obtained_at         TEXT    NOT NULL,
  expires_at          TEXT,                -- 预计失效（30 天）
  valid               INTEGER NOT NULL DEFAULT 1,
  login_attempts_left INTEGER,
  last_login_attempt  TEXT,
  last_relogin_at     TEXT,                -- 上次自动重登录时间
  relogin_state       TEXT                 -- ok|retrying|failed|blocked
);

CREATE TABLE panel_session (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
```

- [ ] **Step 4: 实现 `src/bt169/db.py`**

```python
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

        # 写连接：isolation_level=None → 自己控制事务
        self._writer = self._connect()
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")

    # ---------- 连接 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
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

    # ---------- 迁移 ----------

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
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 13 passed

- [ ] **Step 6: 提交**

```bash
git add src/bt169/db.py src/bt169/migrations/001_init.sql tests/test_db.py
git commit -m "feat(db): SQLite 单写并发模型与初始 schema 迁移"
```

---

## Task 5: `models.py` —— 领域对象与 DTO

**Files:**
- Create: `src/bt169/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `PostStatus`（`Literal`）、`VALID_STATUSES: frozenset[str]`、`BROWSABLE_STATUSES: frozenset[str]`
  - `@dataclass(frozen=True, slots=True) class Post` —— 字段名与 DB 列一致（`cover_img` / `detail_img`）
  - `Post.from_row(row: sqlite3.Row) -> Post`
  - `@dataclass(frozen=True, slots=True) class PostDTO` —— API 输出，字段名 `cover` / `detail`
  - `PostDTO.from_post(post, *, emby_in_library: bool = False) -> PostDTO`、`PostDTO.to_dict()`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_models.py
import sqlite3

import pytest

from bt169.models import BROWSABLE_STATUSES, VALID_STATUSES, Post, PostDTO

BASE = {
    "tid": 3986000, "title": "t", "code": "START-624", "actress": "本庄鈴",
    "release_date": "2026-09-17", "size": "7GB",
    "cover_img": "https://img/c.jpg", "detail_img": "https://img/d.jpg",
    "ed2k": "ed2k://|file|x.mkv|1|AA|/", "post_date": "2026-09-14",
    "post_time": None, "status": "done", "retry_count": 0,
    "last_error": None, "next_retry_at": None, "emby_status": None,
    "emby_item_id": None, "emby_checked": None, "tg_sent_at": None,
    "created_at": "2026-09-14T10:00:00+08:00",
    "updated_at": "2026-09-14T10:00:00+08:00",
}


def row(**over):
    """用**真实迁移的 schema** 造一行，避免手写列名漂移。

    用 ``:memory:`` 而不是临时文件：快，且不泄漏临时目录。
    """
    from bt169.db import _MIGRATIONS_DIR

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((_MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8"))

    data = {**BASE, **over}
    cols = ", ".join(data)
    ph = ", ".join("?" * len(data))
    conn.execute(f"INSERT INTO posts ({cols}) VALUES ({ph})", tuple(data.values()))
    return conn.execute("SELECT * FROM posts").fetchone()


def test_from_row():
    p = Post.from_row(row())
    assert p.tid == 3986000
    assert p.cover_img == "https://img/c.jpg"
    assert p.status == "done"


def test_from_row_covers_every_column():
    """Post 必须能吸收 schema 的**全部**列（漏一列就 TypeError）。"""
    p = Post.from_row(row())
    assert p.tg_sent_at is None
    assert p.emby_checked is None


def test_dto_renames_cover_and_detail():
    """ADR-11：前端消费 cover/detail，不是 cover_img/detail_img。"""
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    assert data["cover"] == "https://img/c.jpg"
    assert data["detail"] == "https://img/d.jpg"
    assert "cover_img" not in data
    assert "detail_img" not in data


def test_dto_excludes_internal_fields():
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    for leaked in ("retry_count", "last_error", "next_retry_at", "emby_item_id",
                   "emby_checked", "emby_status", "created_at", "updated_at"):
        assert leaked not in data, f"{leaked} 不应暴露给前端"


def test_dto_exact_key_set():
    """锁定契约（ADR-16）：键集合变化必须是显式决定。"""
    data = PostDTO.from_post(Post.from_row(row())).to_dict()
    assert set(data) == {
        "tid", "title", "code", "actress", "release_date", "size",
        "cover", "detail", "ed2k", "post_date", "status", "emby_in_library",
    }


def test_dto_emby_flag():
    p = Post.from_row(row())
    assert PostDTO.from_post(p).to_dict()["emby_in_library"] is False
    assert PostDTO.from_post(p, emby_in_library=True).to_dict()["emby_in_library"] is True


def test_valid_statuses():
    assert VALID_STATUSES == {"pending", "thanked", "done", "failed", "nolink"}


def test_browsable_is_only_done():
    assert BROWSABLE_STATUSES == {"done"}


def test_dto_handles_null_ed2k():
    data = PostDTO.from_post(Post.from_row(row(ed2k=None, status="nolink"))).to_dict()
    assert data["ed2k"] is None
    assert data["status"] == "nolink"


def test_post_is_frozen():
    p = Post.from_row(row())
    with pytest.raises(Exception):
        p.tid = 1  # type: ignore[misc]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.models'`

- [ ] **Step 3: 实现 `src/bt169/models.py`**

```python
"""领域对象与 API DTO。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

__all__ = [
    "PostStatus", "VALID_STATUSES", "BROWSABLE_STATUSES", "Post", "PostDTO",
]

PostStatus = Literal["pending", "thanked", "done", "failed", "nolink"]

#: 全部合法状态。
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "thanked", "done", "failed", "nolink"}
)

#: 浏览视图只展示这些状态。
BROWSABLE_STATUSES: frozenset[str] = frozenset({"done"})


@dataclass(frozen=True, slots=True)
class Post:
    """领域对象。字段名与 ``posts`` 表列名**一一对应**。"""

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover_img: str | None
    detail_img: str | None
    ed2k: str | None
    post_date: str
    post_time: str | None
    status: str
    retry_count: int
    last_error: str | None
    next_retry_at: str | None
    emby_status: str | None
    emby_item_id: str | None
    emby_checked: str | None
    tg_sent_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Post:
        """从 ``sqlite3.Row`` 构造。

        按字段名逐个取值（而非 ``**row``），这样列名漂移会立刻
        ``KeyError`` 而不是静默丢数据。
        """
        return cls(**{f.name: row[f.name] for f in fields(cls)})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PostDTO:
    """API 输出对象。

    字段名对齐前端 ``app.js`` 的既有消费方式（ADR-11）：
    ``cover_img`` → ``cover``，``detail_img`` → ``detail``。
    内部字段（重试、错误、时间戳）**不外泄**。
    """

    tid: int
    title: str
    code: str | None
    actress: str | None
    release_date: str | None
    size: str | None
    cover: str | None
    detail: str | None
    ed2k: str | None
    post_date: str
    status: str
    emby_in_library: bool

    @classmethod
    def from_post(cls, post: Post, *, emby_in_library: bool = False) -> PostDTO:
        return cls(
            tid=post.tid,
            title=post.title,
            code=post.code,
            actress=post.actress,
            release_date=post.release_date,
            size=post.size,
            cover=post.cover_img,
            detail=post.detail_img,
            ed2k=post.ed2k,
            post_date=post.post_date,
            status=post.status,
            emby_in_library=emby_in_library,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add src/bt169/models.py tests/test_models.py
git commit -m "feat(models): 领域对象与 PostDTO 字段映射"
```

---

## Task 6: `repo/archive.py` —— 归档日期与相邻导航

**Files:**
- Create: `src/bt169/repo/__init__.py`（空）
- Create: `src/bt169/repo/archive.py`
- Test: `tests/test_repo_archive.py`

**Interfaces:**
- Consumes: `Database`、`BROWSABLE_STATUSES`
- Produces:
  - `@dataclass(frozen=True, slots=True) class DateCount`：`date: str`、`count: int`；`to_dict()`
  - `class ArchiveRepo`，构造 `ArchiveRepo(db: Database)`
  - `ArchiveRepo.list_dates() -> list[DateCount]` —— **升序**，只含非空日期
  - `ArchiveRepo.latest_date() -> str | None`
  - `ArchiveRepo.count_for(date: str) -> int`
  - `ArchiveRepo.neighbour(date: str, direction: int) -> str | None` —— `-1` 更早 / `+1` 更晚

- [ ] **Step 1: 写失败测试**

```python
# tests/test_repo_archive.py
import pytest

from bt169.db import Database
from bt169.repo.archive import ArchiveRepo


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield ArchiveRepo(db), db
    db.close()


def insert(db, tid, post_date, status="done"):
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,'2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')",
            (tid, f"t{tid}", post_date, status),
        )


def test_empty(repo):
    ar, _ = repo
    assert ar.list_dates() == []
    assert ar.latest_date() is None


def test_list_dates_ascending_with_counts(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-10")
    insert(db, 3, "2026-09-14")
    got = ar.list_dates()
    assert [d.date for d in got] == ["2026-09-10", "2026-09-14"]
    assert [d.count for d in got] == [2, 1]


def test_only_done_status_is_browsable(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10", status="done")
    insert(db, 2, "2026-09-10", status="pending")
    insert(db, 3, "2026-09-10", status="nolink")
    assert ar.count_for("2026-09-10") == 1


def test_neighbour_skips_empty_gaps(repo):
    """C-10：日期跳转必须跳过无帖日期。"""
    ar, db = repo
    for tid, d in enumerate(["2026-09-01", "2026-09-05", "2026-09-14"], start=1):
        insert(db, tid, d)
    assert ar.neighbour("2026-09-05", -1) == "2026-09-01"
    assert ar.neighbour("2026-09-05", +1) == "2026-09-14"


def test_neighbour_boundaries_return_none(repo):
    """W-12：首/尾日期时对应按钮应禁用 → 返回 None。"""
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    assert ar.neighbour("2026-09-10", -1) is None
    assert ar.neighbour("2026-09-14", +1) is None


def test_neighbour_unknown_date(repo):
    """未知日期的语义：返回**严格**早于/晚于该日期的第一个有帖日期。

    前端不会从无效日期导航（它会先回退到最新日期），所以这是退化情况，
    但语义必须一致——否则“上一天”按钮会突然跳到无关位置。
    """
    ar, db = repo
    insert(db, 1, "2026-09-10")
    assert ar.neighbour("1999-01-01", -1) is None          # 比最早的还早
    assert ar.neighbour("1999-01-01", +1) == "2026-09-10"  # 比它晚的第一个
    assert ar.neighbour("2099-01-01", +1) is None          # 比最晚的还晚
    assert ar.neighbour("2099-01-01", -1) == "2026-09-10"  # 比它早的第一个


def test_neighbour_rejects_bad_direction(repo):
    ar, _ = repo
    with pytest.raises(ValueError, match="direction"):
        ar.neighbour("2026-09-10", 0)


def test_latest_date(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    assert ar.latest_date() == "2026-09-14"


def test_hard_deleted_row_disappears_from_dates(repo):
    """硬删除（ADR-7）：删行后该日期若空则应从列表消失。"""
    ar, db = repo
    insert(db, 1, "2026-09-10")
    insert(db, 2, "2026-09-14")
    with db.write() as c:
        c.execute("DELETE FROM posts WHERE tid=1")
    assert [d.date for d in ar.list_dates()] == ["2026-09-14"]


def test_date_count_to_dict(repo):
    ar, db = repo
    insert(db, 1, "2026-09-10")
    assert ar.list_dates()[0].to_dict() == {"date": "2026-09-10", "count": 1}
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_repo_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.repo'`

- [ ] **Step 3: 建空 `src/bt169/repo/__init__.py`**

```bash
mkdir -p src/bt169/repo && touch src/bt169/repo/__init__.py
```

- [ ] **Step 4: 实现 `src/bt169/repo/archive.py`**

```python
"""归档日期与相邻导航。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from bt169.db import Database
from bt169.models import BROWSABLE_STATUSES

__all__ = ["DateCount", "ArchiveRepo"]

_STATUSES = tuple(sorted(BROWSABLE_STATUSES))
_PH = ", ".join("?" * len(_STATUSES))


@dataclass(frozen=True, slots=True)
class DateCount:
    date: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchiveRepo:
    """归档日期查询。只统计可浏览状态（``done``）的帖子。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def list_dates(self) -> list[DateCount]:
        """返回有帖的日期，**升序**（左旧右新）。"""
        rows = self._db.read().execute(
            f"SELECT post_date AS d, COUNT(*) AS c FROM posts"
            f" WHERE status IN ({_PH}) GROUP BY post_date ORDER BY post_date ASC",
            _STATUSES,
        ).fetchall()
        return [DateCount(date=r["d"], count=r["c"]) for r in rows]

    def latest_date(self) -> str | None:
        row = self._db.read().execute(
            f"SELECT MAX(post_date) AS d FROM posts WHERE status IN ({_PH})",
            _STATUSES,
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def count_for(self, date: str) -> int:
        row = self._db.read().execute(
            f"SELECT COUNT(*) AS c FROM posts"
            f" WHERE post_date=? AND status IN ({_PH})",
            (date, *_STATUSES),
        ).fetchone()
        return int(row["c"])

    def neighbour(self, date: str, direction: int) -> str | None:
        """相邻的**有帖**日期。

        用 ``>`` / ``<`` 而非日期算术——这样无帖的日期被自然跳过
        （实测 `data.js` 只有 5 个日期，中间存在空档）。

        Args:
            date: 当前日期 ``YYYY-MM-DD``。
            direction: ``-1`` 更早，``+1`` 更晚。

        Returns:
            相邻日期，或 ``None``（已在端点 / 日期不存在）。
        """
        if direction not in (-1, 1):
            raise ValueError(f"direction 必须是 -1 或 +1，收到 {direction}")

        op = "<" if direction < 0 else ">"
        order = "DESC" if direction < 0 else "ASC"
        row = self._db.read().execute(
            f"SELECT post_date AS d FROM posts"
            f" WHERE status IN ({_PH}) AND post_date {op} ?"
            f" GROUP BY post_date ORDER BY post_date {order} LIMIT 1",
            (*_STATUSES, date),
        ).fetchone()
        return row["d"] if row else None
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_repo_archive.py -v`
Expected: 9 passed

- [ ] **Step 6: 提交**

```bash
git add src/bt169/repo/__init__.py src/bt169/repo/archive.py tests/test_repo_archive.py
git commit -m "feat(repo): 归档日期列表与相邻日期导航"
```

---

## Task 7: `repo/posts.py` —— 帖子查询与硬删除

**Files:**
- Create: `src/bt169/repo/posts.py`
- Test: `tests/test_repo_posts.py`

**Interfaces:**
- Consumes: `Database`、`Post`、`PostDTO`、`BROWSABLE_STATUSES`、`VALID_STATUSES`
- Produces:
  - `class PostRepo`，构造 `PostRepo(db: Database)`
  - `PostRepo.list_by_date(date: str) -> list[Post]` —— 只含可浏览状态，按 `tid DESC`
  - `PostRepo.get(tid: int) -> Post | None`
  - `PostRepo.delete(tid: int) -> bool` —— **硬删除**，返回是否删到了行
  - `PostRepo.set_status(tid, status, *, error=None, next_retry_at=None) -> None`
  - `PostRepo.set_emby(tid, *, in_library: bool, item_id: str | None = None, checked_at: str) -> None`
  - `PostRepo.mark_tg_sent(tid: int, when: str) -> None`
  - `PostRepo.count_by_status() -> dict[str, int]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_repo_posts.py
import pytest

from bt169.db import Database
from bt169.repo.posts import PostRepo

NOW = "2026-09-18T12:00:00+08:00"


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield PostRepo(db), db
    db.close()


def insert(db, tid, post_date="2026-09-14", status="done", code="START-624", **over):
    cols = {
        "tid": tid, "title": f"t{tid}", "post_date": post_date, "status": status,
        "code": code, "ed2k": "ed2k://|file|x.mkv|1|AA|/",
        "cover_img": "https://img/c.jpg", "detail_img": "https://img/d.jpg",
        "created_at": NOW, "updated_at": NOW, **over,
    }
    names = ", ".join(cols)
    ph = ", ".join("?" * len(cols))
    with db.write() as c:
        c.execute(f"INSERT INTO posts ({names}) VALUES ({ph})", tuple(cols.values()))


def test_list_by_date_only_browsable(repo):
    pr, db = repo
    insert(db, 1, status="done")
    insert(db, 2, status="pending")
    insert(db, 3, status="nolink")
    assert [p.tid for p in pr.list_by_date("2026-09-14")] == [1]


def test_list_by_date_newest_tid_first(repo):
    pr, db = repo
    insert(db, 100)
    insert(db, 300)
    insert(db, 200)
    assert [p.tid for p in pr.list_by_date("2026-09-14")] == [300, 200, 100]


def test_list_by_date_empty(repo):
    pr, _ = repo
    assert pr.list_by_date("2026-09-14") == []


def test_get(repo):
    pr, db = repo
    insert(db, 3986000)
    p = pr.get(3986000)
    assert p is not None and p.code == "START-624"
    assert pr.get(999) is None


def test_delete_is_hard(repo):
    """ADR-7：硬删除 —— 行必须真的消失，不留 deleted_at。"""
    pr, db = repo
    insert(db, 1)
    assert pr.delete(1) is True
    assert pr.get(1) is None
    n = db.read().execute("SELECT COUNT(*) FROM posts WHERE tid=1").fetchone()[0]
    assert n == 0


def test_delete_missing_returns_false(repo):
    pr, _ = repo
    assert pr.delete(999) is False


def test_delete_is_idempotent(repo):
    pr, db = repo
    insert(db, 1)
    assert pr.delete(1) is True
    assert pr.delete(1) is False


def test_set_status(repo):
    pr, db = repo
    insert(db, 1, status="pending")
    pr.set_status(1, "failed", error="boom", next_retry_at=NOW)
    p = pr.get(1)
    assert p.status == "failed"
    assert p.last_error == "boom"
    assert p.next_retry_at == NOW


def test_set_status_rejects_unknown(repo):
    pr, db = repo
    insert(db, 1)
    with pytest.raises(ValueError, match="状态"):
        pr.set_status(1, "bogus")


def test_set_status_clears_error_on_success(repo):
    pr, db = repo
    insert(db, 1, status="failed", last_error="boom")
    pr.set_status(1, "done")
    p = pr.get(1)
    assert p.status == "done"
    assert p.last_error is None
    assert p.next_retry_at is None


def test_set_emby(repo):
    pr, db = repo
    insert(db, 1)
    pr.set_emby(1, in_library=True, item_id="abc123", checked_at=NOW)
    p = pr.get(1)
    assert p.emby_status == "in_library"
    assert p.emby_item_id == "abc123"
    assert p.emby_checked == NOW


def test_set_emby_not_in_library(repo):
    pr, db = repo
    insert(db, 1)
    pr.set_emby(1, in_library=False, checked_at=NOW)
    p = pr.get(1)
    assert p.emby_status == "none"
    assert p.emby_item_id is None


def test_mark_tg_sent(repo):
    pr, db = repo
    insert(db, 1)
    assert pr.get(1).tg_sent_at is None
    pr.mark_tg_sent(1, NOW)
    assert pr.get(1).tg_sent_at == NOW


def test_count_by_status(repo):
    pr, db = repo
    insert(db, 1, status="done")
    insert(db, 2, status="done")
    insert(db, 3, status="pending")
    assert pr.count_by_status() == {"done": 2, "pending": 1}


def test_updated_at_bumped_on_write(repo):
    pr, db = repo
    insert(db, 1, status="pending")
    before = pr.get(1).updated_at
    pr.set_status(1, "done")
    assert pr.get(1).updated_at != before
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_repo_posts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.repo.posts'`

- [ ] **Step 3: 实现 `src/bt169/repo/posts.py`**

```python
"""帖子读写。"""

from __future__ import annotations

from datetime import datetime

from bt169.db import Database
from bt169.models import BROWSABLE_STATUSES, VALID_STATUSES, Post

__all__ = ["PostRepo", "now_iso"]

_STATUSES = tuple(sorted(BROWSABLE_STATUSES))
_PH = ", ".join("?" * len(_STATUSES))


def now_iso() -> str:
    """当前时间，ISO 8601 带 UTC+8 偏移。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class PostRepo:
    """帖子查询与状态变更。

    浏览路径**只**返回 ``done`` 状态的帖子；其他状态是采集内部状态。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------- 读 ----------

    def list_by_date(self, date: str) -> list[Post]:
        """某归档日的帖子，**新帖在前**（``tid DESC``）。"""
        rows = self._db.read().execute(
            f"SELECT * FROM posts WHERE post_date=? AND status IN ({_PH})"
            f" ORDER BY tid DESC",
            (date, *_STATUSES),
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def get(self, tid: int) -> Post | None:
        row = self._db.read().execute(
            "SELECT * FROM posts WHERE tid=?", (tid,)
        ).fetchone()
        return Post.from_row(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        rows = self._db.read().execute(
            "SELECT status, COUNT(*) AS c FROM posts GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ---------- 写 ----------

    def delete(self, tid: int) -> bool:
        """**硬删除**一行（ADR-7）。

        图片删除不在这里做——`imagecache` 需要先做引用计数，
        见 `ARCHITECTURE.md` §5.8。

        Returns:
            True 表示删掉了行；False 表示本来就不存在（幂等）。
        """
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM posts WHERE tid=?", (tid,))
            return cur.rowcount > 0

    def set_status(
        self,
        tid: int,
        status: str,
        *,
        error: str | None = None,
        next_retry_at: str | None = None,
    ) -> None:
        """更新状态机。

        转到 ``done`` 时**自动清空** ``last_error`` / ``next_retry_at``
        ——残留的错误信息会误导 `doctor` 与前端状态显示。
        """
        if status not in VALID_STATUSES:
            raise ValueError(
                f"未知状态：{status!r}（合法值：{sorted(VALID_STATUSES)}）"
            )
        if status == "done":
            error = None
            next_retry_at = None

        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET status=?, last_error=?, next_retry_at=?,"
                " retry_count=retry_count+1, updated_at=? WHERE tid=?",
                (status, error, next_retry_at, now_iso(), tid),
            )

    def set_emby(
        self,
        tid: int,
        *,
        in_library: bool,
        item_id: str | None = None,
        checked_at: str,
    ) -> None:
        """记录 Emby 查询结果（缓存，避免每次浏览都打 Emby）。"""
        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET emby_status=?, emby_item_id=?, emby_checked=?,"
                " updated_at=? WHERE tid=?",
                (
                    "in_library" if in_library else "none",
                    item_id if in_library else None,
                    checked_at,
                    now_iso(),
                    tid,
                ),
            )

    def mark_tg_sent(self, tid: int, when: str) -> None:
        with self._db.write() as conn:
            conn.execute(
                "UPDATE posts SET tg_sent_at=?, updated_at=? WHERE tid=?",
                (when, now_iso(), tid),
            )
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_repo_posts.py -v`
Expected: 15 passed

- [ ] **Step 5: 提交**

```bash
git add src/bt169/repo/posts.py tests/test_repo_posts.py
git commit -m "feat(repo): 帖子查询、状态机与硬删除"
```

---

## Task 8: `repo/settings.py` —— KV 配置（加密 + 脱敏）

**Files:**
- Create: `src/bt169/repo/settings.py`
- Test: `tests/test_repo_settings.py`

**Interfaces:**
- Consumes: `Database`、`SecretBox`、`SECRET_FIELDS`、`PLACEHOLDER`、`SETTINGS_SECTIONS`
- Produces:
  - `class SettingsRepo`，构造 `SettingsRepo(db: Database, box: SecretBox)`
  - `SettingsRepo.get(key: str, default: str | None = None) -> str | None` —— **解密后**返回明文
  - `SettingsRepo.get_all(*, masked: bool = False) -> dict[str, str]` —— `masked=True` 时密钥字段返回 `PLACEHOLDER`
  - `SettingsRepo.put(key: str, value: str) -> None` —— 自动加密密钥字段
  - `SettingsRepo.put_many(items: dict[str, str]) -> list[str]` —— 跳过等于 `PLACEHOLDER` 的值，返回被跳过的键
  - `SettingsRepo.delete(key: str) -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_repo_settings.py
import pytest

from bt169.config import PLACEHOLDER
from bt169.crypto import SecretBox
from bt169.db import Database
from bt169.repo.settings import SettingsRepo

KEY = b"\x07" * 32


@pytest.fixture
def repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield SettingsRepo(db, SecretBox(KEY)), db
    db.close()


def test_put_get_roundtrip(repo):
    sr, _ = repo
    sr.put("site.rss_url", "https://169bt.com/forum.php?fid=192")
    assert sr.get("site.rss_url") == "https://169bt.com/forum.php?fid=192"


def test_get_default(repo):
    sr, _ = repo
    assert sr.get("missing") is None
    assert sr.get("missing", "d") == "d"


def test_secret_is_encrypted_at_rest(repo):
    """密钥字段落库必须是密文，且标记 encrypted=1。"""
    sr, db = repo
    sr.put("site.password", "hunter2")
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.password'"
    ).fetchone()
    assert row["encrypted"] == 1
    assert "hunter2" not in row["value"]
    assert sr.get("site.password") == "hunter2"          # 读出来是明文


def test_non_secret_stays_plaintext(repo):
    sr, db = repo
    sr.put("site.rss_url", "x")
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.rss_url'"
    ).fetchone()
    assert row["encrypted"] == 0
    assert row["value"] == "x"


def test_all_secret_fields_are_encrypted(repo):
    sr, db = repo
    for k in ("site.password", "tg.token", "emby.apikey"):
        sr.put(k, f"v-{k}")
        row = db.read().execute(
            "SELECT value, encrypted FROM settings WHERE key=?", (k,)
        ).fetchone()
        assert row["encrypted"] == 1, k
        assert f"v-{k}" not in row["value"], k


def test_non_secret_suffix_is_not_encrypted(repo):
    """`panel_password` 最后一段不是 `password` → 不应被当作密钥字段。"""
    sr, db = repo
    sr.put("basic.panel_password_hash", "x")
    row = db.read().execute(
        "SELECT encrypted FROM settings WHERE key='basic.panel_password_hash'"
    ).fetchone()
    assert row["encrypted"] == 0


def test_get_all_returns_plaintext_by_default(repo):
    sr, _ = repo
    sr.put("site.password", "hunter2")
    sr.put("site.rss_url", "x")
    assert sr.get_all() == {"site.password": "hunter2", "site.rss_url": "x"}


def test_get_all_masked_hides_secrets(repo):
    """脱敏读取：密钥字段变占位符，非密钥字段照旧。"""
    sr, _ = repo
    sr.put("site.password", "hunter2")
    sr.put("site.rss_url", "x")
    got = sr.get_all(masked=True)
    assert got["site.password"] == PLACEHOLDER
    assert got["site.rss_url"] == "x"
    assert "hunter2" not in str(got)


def test_put_many_skips_placeholder(repo):
    """提交时等于占位符 → 视为「未修改」，保留旧值。"""
    sr, _ = repo
    sr.put("site.password", "old-secret")
    skipped = sr.put_many({"site.password": PLACEHOLDER, "site.rss_url": "new"})
    assert skipped == ["site.password"]
    assert sr.get("site.password") == "old-secret"
    assert sr.get("site.rss_url") == "new"


def test_put_many_updates_real_value(repo):
    sr, _ = repo
    sr.put("site.password", "old")
    sr.put_many({"site.password": "new"})
    assert sr.get("site.password") == "new"


def test_put_overwrites(repo):
    sr, _ = repo
    sr.put("site.username", "a")
    sr.put("site.username", "b")
    assert sr.get("site.username") == "b"


def test_sections_do_not_collide_at_repo_level(repo):
    """★ 回归：两个分区的同名 key 必须互不影响。"""
    sr, _ = repo
    sr.put("site.password", "forum")
    sr.put("proxy.password", "proxy")
    assert sr.get("site.password") == "forum"
    assert sr.get("proxy.password") == "proxy"


def test_put_updates_timestamp(repo):
    sr, db = repo
    sr.put("site.k", "a")
    first = db.read().execute(
        "SELECT updated_at FROM settings WHERE key='site.k'"
    ).fetchone()[0]
    sr.put("site.k", "b")
    second = db.read().execute(
        "SELECT updated_at FROM settings WHERE key='site.k'"
    ).fetchone()[0]
    assert second >= first


def test_delete(repo):
    sr, _ = repo
    sr.put("site.k", "a")
    sr.delete("site.k")
    assert sr.get("site.k") is None


def test_wrong_key_raises_on_read(repo, tmp_path):
    """换密钥后读旧密文必须报错，而不是返回垃圾。"""
    from bt169.crypto import DecryptError
    sr, db = repo
    sr.put("site.password", "hunter2")
    other = SettingsRepo(db, SecretBox(b"\x08" * 32))
    with pytest.raises(DecryptError):
        other.get("site.password")
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_repo_settings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.repo.settings'`

- [ ] **Step 3: 实现 `src/bt169/repo/settings.py`**

```python
"""KV 配置读写。密钥字段加密落库、脱敏读取。"""

from __future__ import annotations

from bt169.config import PLACEHOLDER, is_secret
from bt169.crypto import SecretBox
from bt169.db import Database
from bt169.repo.posts import now_iso

__all__ = ["SettingsRepo"]


class SettingsRepo:
    """``settings`` 表的读写封装。

    - 写入时，键名最后一段在 :data:`SECRET_FIELDS` 中的值自动 AES-256-GCM 加密，
      并置 ``encrypted=1``。
    - 读取时透明解密；``get_all(masked=True)`` 把密钥字段替换为
      :data:`PLACEHOLDER`，供 API 返回给前端。
    - ``put_many`` 跳过等于占位符的值——前端回传未修改的密钥字段时
      不应把占位符写进数据库覆盖真实密钥。
    - **键名必须带分区前缀**（``site.password``，见 :func:`section_key`）。
    """

    def __init__(self, db: Database, box: SecretBox) -> None:
        self._db = db
        self._box = box

    # ---------- 读 ----------

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

    # ---------- 写 ----------

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
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_repo_settings.py -v`
Expected: 15 passed

- [ ] **Step 5: 提交**

```bash
git add src/bt169/repo/settings.py tests/test_repo_settings.py
git commit -m "feat(repo): KV 配置加密落库与脱敏读取"
```

---

## Task 9: `api/deps.py` + `api/app.py` + 健康检查

**Files:**
- Create: `src/bt169/api/__init__.py`（空）
- Create: `src/bt169/api/deps.py`
- Create: `src/bt169/api/app.py`
- Create: `src/bt169/api/routes/__init__.py`（空）
- Create: `src/bt169/api/routes/health.py`
- Create: `tests/conftest.py`
- Test: `tests/test_api.py`（本任务只写健康检查部分）

**Interfaces:**
- Consumes: `Database`、`SecretBox`、`SettingsRepo`、`PostRepo`、`ArchiveRepo`
- Produces:
  - `api.deps.get_db(request) -> Database`
  - `api.deps.get_posts(request) -> PostRepo`
  - `api.deps.get_archive(request) -> ArchiveRepo`
  - `api.deps.get_settings(request) -> SettingsRepo`
  - `api.app.create_app(db, *, box, ui_dir=None) -> FastAPI`
  - pytest fixtures：`db`、`box`、`client`

- [ ] **Step 1: 写 `tests/conftest.py`**

```python
# tests/conftest.py
import pytest
from fastapi.testclient import TestClient

from bt169.crypto import SecretBox
from bt169.db import Database

TEST_KEY = b"\x09" * 32


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.migrate()
    yield d
    d.close()


@pytest.fixture
def box():
    return SecretBox(TEST_KEY)


@pytest.fixture
def client(db, box):
    from bt169.api.app import create_app
    app = create_app(db, box=box, ui_dir=None)   # None = 不挂静态，只测 API
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seed(db):
    """往库里塞一条可浏览的帖子，返回 tid。"""
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,code,actress,release_date,size,"
            " cover_img,detail_img,ed2k,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (3986000, "START-624 本庄鈴", "START-624", "本庄鈴", "2026-09-17", "7GB",
             "https://img/c.jpg", "https://img/d.jpg",
             "ed2k://|file|x.mkv|1|AA|/", "2026-09-14", "done",
             now_iso(), now_iso()),
        )
    return 3986000
```

- [ ] **Step 2: 写 `tests/test_api.py` 的健康检查部分**

```python
# tests/test_api.py
def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_needs_no_auth(client):
    """健康检查供 Lucky/存活探测使用，不能要求认证。"""
    assert client.get("/api/health").status_code == 200
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.api'`

- [ ] **Step 4: 建包目录**

```bash
mkdir -p src/bt169/api/routes
touch src/bt169/api/__init__.py src/bt169/api/routes/__init__.py
```

- [ ] **Step 5: 实现 `src/bt169/api/deps.py`**

```python
"""FastAPI 依赖注入。

所有依赖从 ``request.app.state`` 取——不引入全局单例，
这样测试可以并行创建多个 app 实例而互不干扰。
"""

from __future__ import annotations

from fastapi import Request

from bt169.db import Database
from bt169.repo.archive import ArchiveRepo
from bt169.repo.posts import PostRepo
from bt169.repo.settings import SettingsRepo

__all__ = ["get_db", "get_posts", "get_archive", "get_settings"]


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_posts(request: Request) -> PostRepo:
    return PostRepo(request.app.state.db)


def get_archive(request: Request) -> ArchiveRepo:
    return ArchiveRepo(request.app.state.db)


def get_settings(request: Request) -> SettingsRepo:
    return SettingsRepo(request.app.state.db, request.app.state.box)
```

- [ ] **Step 6: 实现 `src/bt169/api/routes/health.py`**

```python
"""健康检查。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from bt169 import __version__

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> dict[str, Any]:
    """存活探测。

    **不需要认证**：Lucky 反代与容器健康检查会匿名访问。
    只报告「进程活着」，不含任何敏感信息——会话/采集器状态在
    ``/api/status``（需要认证）。
    """
    return {"status": "ok", "version": __version__}
```

- [ ] **Step 7: 实现 `src/bt169/api/app.py`**

```python
"""FastAPI 应用装配。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from bt169 import __version__
from bt169.config import UI_DIR
from bt169.crypto import SecretBox
from bt169.db import Database

__all__ = ["create_app"]


def create_app(
    db: Database,
    *,
    box: SecretBox,
    ui_dir: Path | None = UI_DIR,
) -> FastAPI:
    """装配应用。

    Args:
        db: 已迁移的数据库。
        box: 设置密钥字段的加解密器。
        ui_dir: 前端目录；``None`` 表示不挂载静态资源（API 单测用）。
    """
    app = FastAPI(
        title="169bt 归档台",
        version=__version__,
        docs_url=None,          # 个人自用，不需要 Swagger UI 暴露面
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db = db
    app.state.box = box

    _install_error_handler(app)

    from bt169.api.routes import health

    app.include_router(health.router)

    # 静态资源必须**最后**挂载：Starlette 按注册顺序匹配，
    # 挂在 "/" 的 StaticFiles 会吞掉之后注册的所有路由。
    if ui_dir is not None and Path(ui_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    return app


def _install_error_handler(app: FastAPI) -> None:
    """统一错误响应形状：``{"error": {"code", "message"}}``。

    前端 ``api.js`` 依赖这个形状做错误归一化（`FRONTEND.md` §3.1）。
    """

    @app.exception_handler(Exception)
    async def unhandled(request, exc):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": str(exc)}},
        )
```

- [ ] **Step 8: 运行确认通过**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: 2 passed

- [ ] **Step 9: 提交**

```bash
git add src/bt169/api tests/conftest.py tests/test_api.py
git commit -m "feat(api): 应用装配、依赖注入与健康检查"
```

---

## Task 10: `api/routes/posts.py` —— 只读浏览接口

**Files:**
- Create: `src/bt169/api/routes/posts.py`
- Modify: `src/bt169/api/app.py`（注册路由）
- Test: `tests/test_api.py`（追加）

**Interfaces:**
- Consumes: `PostRepo`、`ArchiveRepo`、`PostDTO`
- Produces:
  - `GET /api/dates` → `200 [{date, count}]`（升序）
  - `GET /api/posts?date=YYYY-MM-DD` → `200 [PostDTO]`
  - 非法日期 → `400`；未知日期 → `200 []`

- [ ] **Step 1: 追加失败测试**

```python
# tests/test_api.py  （追加到文件末尾）
import pytest


def test_dates_empty(client):
    r = client.get("/api/dates")
    assert r.status_code == 200
    assert r.json() == []


def test_dates_lists_ascending(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (111, "older", "2026-09-10", "done", now_iso(), now_iso()),
        )
    assert client.get("/api/dates").json() == [
        {"date": "2026-09-10", "count": 1},
        {"date": "2026-09-14", "count": 1},
    ]


def test_dates_excludes_non_browsable(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (222, "pending", "2026-09-15", "pending", now_iso(), now_iso()),
        )
    dates = [d["date"] for d in client.get("/api/dates").json()]
    assert "2026-09-15" not in dates


def test_posts_by_date(client, seed):
    r = client.get("/api/posts", params={"date": "2026-09-14"})
    assert r.status_code == 200
    posts = r.json()
    assert len(posts) == 1
    assert posts[0]["tid"] == 3986000
    assert posts[0]["code"] == "START-624"


def test_posts_contract_fields(client, seed):
    """ADR-11/16：字段名必须是 cover/detail，且无内部字段。"""
    p = client.get("/api/posts", params={"date": "2026-09-14"}).json()[0]
    assert p["cover"] == "https://img/c.jpg"
    assert p["detail"] == "https://img/d.jpg"
    assert set(p) == {
        "tid", "title", "code", "actress", "release_date", "size",
        "cover", "detail", "ed2k", "post_date", "status", "emby_in_library",
    }


def test_posts_unknown_date_is_empty(client, seed):
    r = client.get("/api/posts", params={"date": "1999-01-01"})
    assert r.status_code == 200
    assert r.json() == []


def test_posts_rejects_bad_date_format(client):
    r = client.get("/api/posts", params={"date": "2026/09/14"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_date"


def test_posts_requires_date(client):
    assert client.get("/api/posts").status_code == 422


def test_posts_newest_first(client, seed, db):
    from bt169.repo.posts import now_iso
    with db.write() as c:
        c.execute(
            "INSERT INTO posts(tid,title,post_date,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (3999000, "newer", "2026-09-14", "done", now_iso(), now_iso()),
        )
    tids = [p["tid"] for p in client.get("/api/posts", params={"date": "2026-09-14"}).json()]
    assert tids == [3999000, 3986000]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL — `404` on `/api/dates`

- [ ] **Step 3: 实现 `src/bt169/api/routes/posts.py`**

```python
"""只读浏览接口：日期列表与按日期取帖。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from bt169.api.deps import get_archive, get_posts
from bt169.models import PostDTO
from bt169.repo.archive import ArchiveRepo
from bt169.repo.posts import PostRepo

router = APIRouter(tags=["posts"])

#: 归档日期必须是严格的 YYYY-MM-DD。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/api/dates")
def list_dates(archive: ArchiveRepo = Depends(get_archive)) -> list[dict[str, Any]]:
    """有帖的归档日期，**升序**（左旧右新）。

    前端用它同时得到两个东西：日期条的内容，以及**上一日/下一日**的目标
    ——因此不需要单独的 nav 接口（W-12 的「跳过空档」由前端在数组上自然满足）。
    """
    return [d.to_dict() for d in archive.list_dates()]


@router.get("/api/posts")
def list_posts(
    date: str = Query(..., description="归档日期 YYYY-MM-DD"),
    posts: PostRepo = Depends(get_posts),
) -> list[dict[str, Any]]:
    """某归档日的帖子，新帖在前。

    未知日期返回**空数组**而非 404——「这天没帖子」不是错误，
    前端不应把它当作失败处理。
    """
    if not _DATE_RE.match(date):
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_date", "message": f"日期格式必须是 YYYY-MM-DD：{date!r}"},
        )

    return [
        PostDTO.from_post(p, emby_in_library=p.emby_status == "in_library").to_dict()
        for p in posts.list_by_date(date)
    ]
```

- [ ] **Step 4: 注册路由**

在 `src/bt169/api/app.py` 中，把 `from bt169.api.routes import health` 一行改为：

```python
    from bt169.api.routes import health, posts

    app.include_router(health.router)
    app.include_router(posts.router)
```

- [ ] **Step 5: 让 HTTPException 保持统一错误形状**

`HTTPException` 默认返回 `{"detail": ...}`，与我们的 `{"error": {...}}` 不一致。
在 `_install_error_handler` 中追加：

```python
    from fastapi.exceptions import HTTPException as FastAPIHTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = {"error": detail}
        else:
            body = {"error": {"code": "http_error", "message": str(detail)}}
        return JSONResponse(status_code=exc.status_code, content=body)
```

> 同时把 `@app.exception_handler(Exception)` 改为
> `@app.exception_handler(FastAPIHTTPException)` 之外的形式保持不变——
> 注意 `Exception` 处理器在 TestClient 中默认会**重新抛出**异常；
> 测试里若看到 500 而非统一形状，检查是否被 `raise_server_exceptions` 拦下。

- [ ] **Step 6: 运行确认通过**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: 11 passed（T9 的 2 项 + 本任务 9 项）

- [ ] **Step 7: 提交**

```bash
git add src/bt169/api/routes/posts.py src/bt169/api/app.py tests/test_api.py
git commit -m "feat(api): 日期列表与按日期取帖接口"
```

---

## Task 11: `api/routes/settings.py` —— 分区读写

**Files:**
- Create: `src/bt169/api/routes/settings.py`
- Modify: `src/bt169/api/app.py`（注册路由）
- Test: `tests/test_api_settings.py`

**Interfaces:**
- Consumes: `SettingsRepo`、`SETTINGS_SECTIONS`、`parse_rss_url`、`PLACEHOLDER`
- Produces:
  - `GET /api/settings` → `200 {section: {key: value}}`，密钥字段为 `PLACEHOLDER`
  - `PUT /api/settings` body `{section, values}` → `200 {section, saved: [...], skipped: [...]}`
  - 未知分区 → `400 bad_section`；非法 RSS URL → `400 bad_rss_url`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_api_settings.py
import pytest

from bt169.config import PLACEHOLDER


def test_get_settings_empty(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {s: {} for s in ("site", "basic", "proxy", "emby", "tg")}


def test_put_then_get_roundtrip(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh"},
    })
    assert r.status_code == 200
    assert r.json()["saved"] == ["username"]
    assert client.get("/api/settings").json()["site"]["username"] == "ymxh"


def test_put_then_get_masks_secret(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"password": "hunter2"},
    })
    assert r.json()["saved"] == ["password"]
    got = client.get("/api/settings").json()
    assert got["site"]["password"] == PLACEHOLDER


def test_basic_section_roundtrip(client):
    """「基础设置」目前只有访问密码——它由认证模块以**哈希**存储，
    不经这个 KV 接口回读（见 ADR-17）。这里用一个普通字段占位验证分区可用。"""
    client.put("/api/settings", json={
        "section": "basic", "values": {"theme": "dark"},
    })
    assert client.get("/api/settings").json()["basic"]["theme"] == "dark"


def test_secret_never_returned_in_plaintext(client):
    client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh", "password": "s3cr3t-pw"},
    })
    body = client.get("/api/settings").text
    assert "s3cr3t-pw" not in body
    assert client.get("/api/settings").json()["site"]["password"] == PLACEHOLDER


def test_non_secret_returned_plaintext(client):
    client.put("/api/settings", json={
        "section": "site", "values": {"username": "ymxh"},
    })
    assert client.get("/api/settings").json()["site"]["username"] == "ymxh"


def test_placeholder_means_unchanged(client, db, box):
    """前端回传未修改的密码 → 后端保留原值，不写占位符。

    ★ 种子数据必须用**带分区前缀**的键，与 API 写入的一致；
    否则断言会因为读到空值而**空洞通过**。
    """
    from bt169.repo.settings import SettingsRepo
    sr = SettingsRepo(db, box)
    sr.put("site.password", "real-secret")

    r = client.put("/api/settings", json={
        "section": "site", "values": {"password": PLACEHOLDER, "username": "new"},
    })
    assert r.json()["skipped"] == ["password"]
    assert sr.get("site.password") == "real-secret"
    assert sr.get("site.username") == "new"


def test_rss_url_is_cleaned_on_save(client, db, box):
    """实测：RSS 匿名可用 → auth 参数必须被剥掉。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={
        "section": "site",
        "values": {"rss_url": "https://169bt.com/forum.php?mod=rss&fid=192&auth=deadbeef"},
    })
    assert SettingsRepo(db, box).get("site.rss_url") == "https://169bt.com/forum.php?fid=192"


def test_rss_url_without_fid_rejected(client):
    r = client.put("/api/settings", json={
        "section": "site", "values": {"rss_url": "https://169bt.com/forum.php?mod=rss"},
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_rss_url"


def test_unknown_section_rejected(client):
    r = client.put("/api/settings", json={"section": "nope", "values": {"a": "b"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_section"


def test_secret_fields_are_encrypted_at_rest(client, db):
    """★ 端到端：经 API 存密码 → 库里必须是密文。"""
    client.put("/api/settings", json={
        "section": "site", "values": {"password": "plaintext-check"},
    })
    row = db.read().execute(
        "SELECT value, encrypted FROM settings WHERE key='site.password'"
    ).fetchone()
    assert row["encrypted"] == 1
    assert "plaintext-check" not in row["value"]


def test_sections_are_isolated(client):
    """ADR-18：一次只改一个分区，其他分区不受影响。"""
    client.put("/api/settings", json={"section": "tg", "values": {"chat_id": "1"}})
    client.put("/api/settings", json={"section": "emby", "values": {"url": "http://e"}})
    got = client.get("/api/settings").json()
    assert got["tg"] == {"chat_id": "1"}
    assert got["emby"] == {"url": "http://e"}
    assert got["proxy"] == {}


def test_missing_body_fields(client):
    assert client.put("/api/settings", json={}).status_code == 422


def test_delete_setting(client, db, box):
    """清空某键（值为空字符串）→ 从库中移除。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={"section": "emby", "values": {"url": "http://e"}})
    client.put("/api/settings", json={"section": "emby", "values": {"url": ""}})
    assert SettingsRepo(db, box).get("emby.url") is None


def test_same_field_name_in_two_sections_does_not_collide(client, db, box):
    """★ 回归：`password` 在「站点」与「代理」都存在。
    没加分区前缀时，写代理密码会静默抹掉论坛密码。"""
    from bt169.repo.settings import SettingsRepo
    client.put("/api/settings", json={
        "section": "site", "values": {"password": "forum-pw"},
    })
    client.put("/api/settings", json={
        "section": "proxy", "values": {"password": "proxy-pw"},
    })

    sr = SettingsRepo(db, box)
    assert sr.get("site.password") == "forum-pw"    # 没被覆盖
    assert sr.get("proxy.password") == "proxy-pw"

    got = client.get("/api/settings").json()
    assert got["site"]["password"] == PLACEHOLDER
    assert got["proxy"]["password"] == PLACEHOLDER
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_api_settings.py -v`
Expected: FAIL — `404` on `/api/settings`

- [ ] **Step 3: 实现 `src/bt169/api/routes/settings.py`**

```python
"""设置读写。按分区提交，密钥字段脱敏。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bt169.api.deps import get_settings
from bt169.config import (
    PLACEHOLDER,
    SETTINGS_SECTIONS,
    ConfigError,
    parse_rss_url,
    section_key,
)
from bt169.repo.settings import SettingsRepo

router = APIRouter(tags=["settings"])


class SettingsPatch(BaseModel):
    """一次提交只针对一个分区（ADR-18）。"""

    section: str = Field(..., description="分区名")
    values: dict[str, str] = Field(default_factory=dict)


@router.get("/api/settings")
def read_settings(sr: SettingsRepo = Depends(get_settings)) -> dict[str, Any]:
    """返回全部分区的设置。

    密钥字段一律替换为固定长度占位符——**不回传明文，也不回传长度或末位**。
    前端把占位符原样回传即表示「未修改」。

    存储键带分区前缀（``site.password``），这里拆回嵌套形状：
    ``{"site": {"password": "••••••••"}}``。
    """
    out: dict[str, dict[str, str]] = {s: {} for s in SETTINGS_SECTIONS}
    for storage_key, value in sr.get_all(masked=True).items():
        section, _, key = storage_key.partition(".")
        if section in out:
            out[section][key] = value
    return out


@router.put("/api/settings")
def write_settings(
    patch: SettingsPatch,
    sr: SettingsRepo = Depends(get_settings),
) -> dict[str, Any]:
    """保存一个分区的设置。

    约定：

    - 值等于 :data:`PLACEHOLDER` 的密钥字段 → 视为未修改，跳过（计入 ``skipped``）
    - 值为**空字符串** → 删除该键
    - ``rss_url`` → 解析并清洗（剥掉 ``auth`` 等无关参数）
    """
    if patch.section not in SETTINGS_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_section",
                "message": f"未知分区：{patch.section!r}"
                           f"（合法值：{list(SETTINGS_SECTIONS)}）",
            },
        )

    to_write: dict[str, str] = {}
    to_delete: list[str] = []

    for key, value in patch.values.items():
        # ★ 加分区前缀，避免「站点」与「代理」的 password 互相覆盖
        storage = section_key(patch.section, key)
        if key == "rss_url" and value:
            try:
                _, value = parse_rss_url(value)
            except ConfigError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_rss_url", "message": str(exc)},
                ) from exc
        if value == "":
            to_delete.append(storage)
        else:
            to_write[storage] = value

    skipped = sr.put_many(to_write)
    for storage in to_delete:
        sr.delete(storage)

    return {
        "section": patch.section,
        # 返回值去掉前缀，前端看到的是分区内字段名
        "saved": sorted(k.partition(".")[2] for k in set(to_write) - set(skipped)),
        "skipped": sorted(k.partition(".")[2] for k in skipped),
        "deleted": sorted(k.partition(".")[2] for k in to_delete),
    }
```

> **注意** `GET /api/settings` 的形状：返回**每个分区都存在**的字典
> （`{section: {}}` 而非只含有值的分区），这样前端渲染五个分区时
> 不需要到处判空。

- [ ] **Step 4: 注册路由**

在 `src/bt169/api/app.py` 中：

```python
    from bt169.api.routes import health, posts, settings

    app.include_router(health.router)
    app.include_router(posts.router)
    app.include_router(settings.router)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_api_settings.py -v`
Expected: 12 passed

- [ ] **Step 6: 提交**

```bash
git add src/bt169/api/routes/settings.py src/bt169/api/app.py tests/test_api_settings.py
git commit -m "feat(api): 设置分区读写与密钥脱敏"
```

---

## Task 12: 删除接口（`DELETE` + `sendBeacon`）

**Files:**
- Modify: `src/bt169/api/routes/posts.py`（追加删除端点）
- Test: `tests/test_api.py`（追加）

**Interfaces:**
- Consumes: `PostRepo.delete`
- Produces:
  - `DELETE /api/posts/{tid}` → `204`（硬删除）
  - `POST /api/posts/{tid}/delete` → `204`（同上，供 `sendBeacon`）
  - 不存在的 tid → `404 not_found`

- [ ] **Step 1: 追加失败测试**

```python
# tests/test_api.py  （追加）
def test_delete_removes_row(client, seed):
    assert client.delete(f"/api/posts/{seed}").status_code == 204
    assert client.get("/api/posts", params={"date": "2026-09-14"}).json() == []


def test_delete_is_hard_in_db(client, seed, db):
    """ADR-7：行必须真的消失。"""
    client.delete(f"/api/posts/{seed}")
    n = db.read().execute("SELECT COUNT(*) FROM posts WHERE tid=?", (seed,)).fetchone()[0]
    assert n == 0


def test_delete_missing_returns_404(client):
    r = client.delete("/api/posts/999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_beacon_delete_works(client, seed):
    """sendBeacon 只能发 POST，语义必须等价于 DELETE。"""
    assert client.post(f"/api/posts/{seed}/delete").status_code == 204
    assert client.get("/api/posts", params={"date": "2026-09-14"}).json() == []


def test_beacon_delete_missing_returns_404(client):
    assert client.post("/api/posts/999999/delete").status_code == 404


def test_delete_removes_date_when_last_post_gone(client, seed):
    """删掉某日最后一帖后，该日期应从 /api/dates 消失。"""
    assert client.get("/api/dates").json() == [{"date": "2026-09-14", "count": 1}]
    client.delete(f"/api/posts/{seed}")
    assert client.get("/api/dates").json() == []


def test_delete_twice_second_is_404(client, seed):
    assert client.delete(f"/api/posts/{seed}").status_code == 204
    assert client.delete(f"/api/posts/{seed}").status_code == 404
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL — `405 Method Not Allowed` on DELETE

- [ ] **Step 3: 追加实现到 `src/bt169/api/routes/posts.py`**

```python
def _delete_or_404(posts: PostRepo, tid: int) -> None:
    """硬删除（ADR-7）。图片清理由 `imagecache` 在 P3 接入时补充。"""
    if not posts.delete(tid):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"帖子不存在：{tid}"},
        )


@router.delete("/api/posts/{tid}", status_code=204)
def delete_post(tid: int, posts: PostRepo = Depends(get_posts)) -> None:
    """硬删除一帖（行 + 图片）。

    前端有 5 s 撤销窗口（`FRONTEND.md` §13）——**窗口内不发请求**，
    因此这里的删除是立即且不可逆的。
    """
    _delete_or_404(posts, tid)


@router.post("/api/posts/{tid}/delete", status_code=204)
def delete_post_beacon(tid: int, posts: PostRepo = Depends(get_posts)) -> None:
    """``sendBeacon`` 兜底端点（页面关闭时无法等待 fetch 完成）。

    语义与 ``DELETE /api/posts/{tid}`` **完全等价**——``sendBeacon`` 只能发
    POST，且无法设置请求头或读取响应。
    """
    _delete_or_404(posts, tid)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: 18 passed（T10 的 11 项 + 本任务 7 项）

- [ ] **Step 5: 提交**

```bash
git add src/bt169/api/routes/posts.py tests/test_api.py
git commit -m "feat(api): 硬删除接口与 sendBeacon 等价端点"
```

---

## Task 13: 静态分发与 `169bt serve`

**Files:**
- Modify: `src/bt169/api/app.py`（若尚未挂载静态则确认）
- Create: `src/bt169/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `create_app`、`Database`、`SecretBox`、`UI_DIR`
- Produces:
  - `python -m bt169 serve [--host 127.0.0.1] [--port 8899]`
  - `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: 写失败测试**

```python
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
```

> `test_doctor_command_runs` 会在项目 `data/` 下建库。若不想污染，
> 用 `monkeypatch.setattr("bt169.config.DB_PATH", tmp_path/"x.db")` 同样处理。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bt169.__main__'`

- [ ] **Step 3: 实现 `src/bt169/__main__.py`**

```python
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

    checks: list[tuple[str, bool, str]] = []

    v = _sys.version_info
    checks.append((
        f"Python {v.major}.{v.minor}.{v.micro}",
        v >= (3, 10),
        "需要 ≥3.10（ddddocr 约束）",
    ))

    checks.append((
        f"SQLite {sqlite3.sqlite_version}",
        sqlite3.sqlite_version_info >= (3, 35, 0),
        "需要 ≥3.35（部分索引 + UPSERT）",
    ))
    checks.append((
        f"sqlite3 threadsafety={sqlite3.threadsafety}",
        sqlite3.threadsafety == 3,
        "需要 3（串行化）才能跨线程共享连接",
    ))

    checks.append((f"数据目录 {config.DATA_DIR.name}/", True, ""))
    checks.append((f"前端目录 {config.UI_DIR.name}/", config.UI_DIR.is_dir(), "缺少 ui/ 目录"))
    checks.append((
        "前端入口 index.html",
        (config.UI_DIR / "index.html").is_file(),
        "缺少 ui/index.html",
    ))

    db = Database(config.DB_PATH)
    try:
        db.migrate()
        mode = db.read().execute("PRAGMA journal_mode").fetchone()[0]
        checks.append((f"journal_mode={mode}", mode.lower() == "wal", "需要 WAL"))
        n = db.read().execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        checks.append((f"帖子总数 {n}", True, ""))
    except Exception as exc:
        checks.append(("数据库可用", False, str(exc)))
    finally:
        db.close()

    key_path = config.DATA_DIR / KEY_FILE_NAME
    checks.append((
        f"主密钥 {key_path.name}",
        True,
        "首次启动时自动生成" if not key_path.exists() else "",
    ))

    width = max(len(name) for name, _, _ in checks)
    ok_all = True
    for name, ok, hint in checks:
        mark = "✓" if ok else "✗"
        line = f"  {mark} {name.ljust(width)}"
        if hint and not ok:
            line += f"  ← {hint}"
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
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: 5 passed

- [ ] **Step 5: 端到端手测（真起服务）**

```bash
cd /Users/mario/Dev/Projects/169bt
.venv/bin/bt169 migrate
.venv/bin/bt169 doctor
```

Expected: `doctor` 每行都是 `✓`（`data/` 目录、`ui/` 目录、`index.html` 都已存在）

- [ ] **Step 6: 起服务并实测 API（含静态分发）**

```bash
cd /Users/mario/Dev/Projects/169bt
.venv/bin/bt169 serve --port 8899 &
sleep 2
curl -s http://127.0.0.1:8899/api/health
echo
curl -s http://127.0.0.1:8899/api/dates
echo
curl -s http://127.0.0.1:8899/api/settings
echo
curl -s -o /dev/null -w "index.html → %{http_code} (%{size_download} B)\n" http://127.0.0.1:8899/
curl -s -o /dev/null -w "styles.css → %{http_code}\n" http://127.0.0.1:8899/styles.css
kill %1
```

Expected:

```
{"status":"ok","version":"0.1.0"}
[]
{"site":{},"basic":{},"proxy":{},"emby":{},"tg":{}}
index.html → 200 (...)
styles.css → 200
```

> ★ **关键验证**：`/api/*` 与静态资源**必须同时**可用。
> 若静态挂载在路由之前，`/api/*` 会返回 404 —— 这就是
> `app.py` 里「静态必须最后挂载」注释的原因。

- [ ] **Step 7: 提交**

```bash
git add src/bt169/__main__.py tests/test_cli.py
git commit -m "feat(cli): serve/migrate/doctor 子命令与主密钥管理"
```

---

## Task 14: 全量回归与文档同步

**Files:**
- Modify: `ARCHITECTURE.md`（§12 路线勾选 P0 状态）
- Modify: `README.md`（新建：如何运行）

**Interfaces:**
- Consumes: 全部
- Produces: 通过的测试套件 + 可照做的运行说明

- [ ] **Step 1: 全量测试**

```bash
cd /Users/mario/Dev/Projects/169bt
.venv/bin/pytest -v
```

Expected: 全部通过（预计 80+ 项）

- [ ] **Step 2: 覆盖率自检（可选但推荐）**

```bash
.venv/bin/pip install -q pytest-cov
.venv/bin/pytest --cov=bt169 --cov-report=term-missing
```

- [ ] **Step 3: 写 `README.md`**

````markdown
# 169bt 归档台

个人自用的 4K 帖归档与 ED2K 提取工具。后端 Python，前端原生 ESM。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/bt169 migrate     # 建库
.venv/bin/bt169 doctor      # 环境自检
.venv/bin/bt169 serve       # → http://127.0.0.1:8899
```

## 命令

| 命令 | 作用 |
|---|---|
| `bt169 serve [--host H] [--port P]` | 启动 Web 服务（默认只监听回环） |
| `bt169 migrate` | 建库 / 升级 schema（幂等） |
| `bt169 doctor` | 环境自检，逐项 ✓/✗ |
| `bt169 --version` | 版本 |

## 测试

```bash
.venv/bin/pytest -v
```

## 文档

| 文档 | 内容 |
|---|---|
| `REQUIREMENTS.md` | 需求与已实测事实 |
| `ARCHITECTURE.md` | 后端架构、数据模型、API 契约、ADR |
| `FRONTEND.md` | 前端架构与技术选型 |
| `ui/DESIGN.md` | UI 设计系统与交互 |
| `docs/superpowers/plans/` | 实施计划 |

## 部署

公网访问由 **Lucky 反代**负责 HTTPS。uvicorn 只监听 `127.0.0.1`。

反代必须注入 `X-Forwarded-Proto: https`，否则 PWA 的 Service Worker
与 manifest 会因为非安全上下文而失败。
````

- [ ] **Step 4: 更新 `ARCHITECTURE.md` §12 路线状态**

在 §12 的 P0 行标注完成：

```markdown
| **P0** | `config` + `db` + 迁移 + `169bt migrate` | ✅ 已完成（见 `docs/superpowers/plans/2026-09-18-backend-foundation.md`） |
```

- [ ] **Step 5: 确认前端未被改动**

```bash
cd /Users/mario/Dev/Projects/169bt
md5 -q ui/app.js ui/index.html ui/settings.js ui/data.js ui/styles.css
```

Expected: 与开工前一致（`app.js` = `3be8e808…`，`index.html` = `5da668ff…`）。
若不一致 → **本计划不应改动任何 `ui/` 文件**，回滚。

- [ ] **Step 6: 提交**

```bash
git add README.md ARCHITECTURE.md
git commit -m "docs: 运行说明与 P0 完成状态"
```

---

## 验收清单

全部完成后，以下每一条都必须能实测通过：

- [ ] `.venv/bin/pytest` 全绿
- [ ] `.venv/bin/bt169 migrate` 幂等（跑两次结果一致）
- [ ] `.venv/bin/bt169 doctor` 全部 `✓`
- [ ] `.venv/bin/bt169 serve` 起得来，`curl /api/health` 返回 `ok`
- [ ] `curl /api/dates` 返回 `[]`（空库）
- [ ] `curl /api/settings` 返回五个分区（均为空对象）
- [ ] 静态资源与 `/api/*` **同时**可用
- [ ] `PUT /api/settings` 存密码后，`GET` 只看到 `••••••••`
- [ ] 硬删除后 `/api/dates` 中该日期消失（若为当日最后一帖）
- [ ] `git log` 中每个任务一个提交
- [ ] `ui/` 目录**零改动**（md5 与开工前一致）
