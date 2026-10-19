# ABOUTME: Reads Slack xoxc tokens from Chromium Local Storage and decrypts the d cookie.
# ABOUTME: Works on copies of the Cookies database and relies on the in-house LevelDB reader.
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from slarc.adapters.leveldb import latest_values

LOCAL_CONFIG_SUFFIX = b"\x00\x01localConfig_v2"
LATIN1_PREFIX = 0x01
UTF16_PREFIX = 0x00
COOKIE_PREFIX = "xoxd-"
DOMAIN_HASH_SIZE = 32
DOMAIN_HASH_MIN_VERSION = 24
DECRYPT_FAILED = "cookie did not decrypt"


@dataclass
class TeamToken:
    team_id: str
    domain: str
    name: str
    token: str = field(repr=False)


def read_local_config(leveldb_dir: Path) -> list[TeamToken]:
    values = latest_values(leveldb_dir, lambda key: key.endswith(LOCAL_CONFIG_SUFFIX))
    tokens: list[TeamToken] = []
    for value in values.values():
        teams = json.loads(_decode_local_storage_value(value)).get("teams", {})
        tokens.extend(
            TeamToken(team_id, team["domain"], team.get("name", ""), team["token"])
            for team_id, team in teams.items()
        )
    return tokens


def _decode_local_storage_value(value: bytes) -> str:
    body = value[1:]
    if value[0] == LATIN1_PREFIX:
        return body.decode("latin-1")
    if value[0] == UTF16_PREFIX:
        return body.decode("utf-16-le")
    raise ValueError("unknown Local Storage value encoding")


def decrypt_cookie(encrypted: bytes, password: bytes, iterations: int, db_version: int) -> str:
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", iterations, 16)
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        if db_version >= DOMAIN_HASH_MIN_VERSION:
            plain = plain[DOMAIN_HASH_SIZE:]
        text = plain.decode("ascii")
    except ValueError:
        raise ValueError(DECRYPT_FAILED) from None
    if not text.startswith(COOKIE_PREFIX):
        raise ValueError(DECRYPT_FAILED)
    return text


def read_d_cookie(cookies_db: Path, password_candidates: list[bytes], iterations: int) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / cookies_db.name
        shutil.copyfile(cookies_db, copy)
        journal = cookies_db.with_name(cookies_db.name + "-journal")
        if journal.exists():
            shutil.copyfile(journal, copy.with_name(copy.name + "-journal"))
        encrypted, db_version = _read_encrypted_cookie(copy)
    for password in password_candidates:
        try:
            return decrypt_cookie(encrypted, password, iterations, db_version)
        except ValueError:
            continue
    raise ValueError(DECRYPT_FAILED)


def _read_encrypted_cookie(db_path: Path) -> tuple[bytes, int]:
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("select value from meta where key = 'version'").fetchone()
        db_version = int(row[0]) if row else 0
        row = db.execute(
            "select encrypted_value from cookies"
            " where name = 'd' and host_key like '%slack.com' and length(encrypted_value) > 0"
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError("no d cookie found")
    return row[0], db_version
