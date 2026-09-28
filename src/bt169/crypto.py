"""对称加密与口令哈希。"""

from __future__ import annotations

import base64
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

__all__ = [
    "SecretBox",
    "DecryptError",
    "hash_password",
    "verify_password",
    "load_secret_box",
]

_NONCE_BYTES = 12          # GCM 推荐 96 bit
_KDF_ITERATIONS = 600_000  # OWASP 对 PBKDF2-SHA256 的建议量级

#: 主密钥文件名（相对 DATA_DIR）。供 load_secret_box 与 CLI 共用。
KEY_FILE_NAME = "169bt.key"


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
            algorithm=hashes.SHA256(), length=32, salt=salt,
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
        except Exception as exc:
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
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return kdf.derive(password.encode("utf-8"))


def load_secret_box() -> SecretBox:
    """读取主密钥；不存在则生成 32 字节随机密钥并写盘（0600）。

    也支持从 ``BT169_SECRET_KEY`` 注入（base64，32 字节）——
    容器化部署时避免把密钥写进镜像层。

    ★ 放在 crypto 而非 ``__main__``：不止 CLI 要密钥——请求处理路径上
    （如 ForumClient 读代理设置）也需要拿到同一个 SecretBox。
    """
    import base64
    import os as _os

    from bt169 import config as _config

    env = _os.environ.get("BT169_SECRET_KEY")
    if env:
        try:
            return SecretBox(base64.b64decode(env, validate=True))
        except Exception as exc:
            raise SystemExit(
                f"BT169_SECRET_KEY 非法（需要 base64 编码的 32 字节）：{exc}"
            ) from exc

    _config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _config.DATA_DIR / KEY_FILE_NAME
    if path.exists():
        raw = base64.b64decode(path.read_text(encoding="ascii").strip())
        return SecretBox(raw)

    raw = _os.urandom(32)
    path.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
    path.chmod(0o600)
    print(f"已生成主密钥：{path}（权限 0600，请勿泄露）")
    return SecretBox(raw)
