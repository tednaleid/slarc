# ABOUTME: Test helper that encrypts cookie values as Chromium does and writes Cookies databases.
# ABOUTME: Lets tests build synthetic Slack desktop app stores without real credentials.
import hashlib
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def encrypt(plaintext: bytes, password: bytes, iterations: int, prefix: bytes = b"v10") -> bytes:
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, 16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(padded) + encryptor.finalize()


def make_cookies_db(path: Path, rows: list[tuple[str, str, bytes]], version: int) -> None:
    """Write a Cookies database holding (host_key, name, encrypted_value) rows."""
    db = sqlite3.connect(path)
    db.execute("create table meta(key text, value text)")
    db.execute("insert into meta values ('version', ?)", (str(version),))
    db.execute("create table cookies(host_key text, name text, value text, encrypted_value blob)")
    db.executemany(
        "insert into cookies values (?, ?, '', ?)",
        [(host_key, name, encrypted) for host_key, name, encrypted in rows],
    )
    db.commit()
    db.close()
