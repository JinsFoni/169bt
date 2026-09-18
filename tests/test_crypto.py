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
    box = SecretBox(KEY)
    assert box.encrypt("same") != box.encrypt("same")


def test_tamper_detected():
    box = SecretBox(KEY)
    tok = bytearray(base64.b64decode(box.encrypt("secret")))
    tok[-1] ^= 0x01
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
