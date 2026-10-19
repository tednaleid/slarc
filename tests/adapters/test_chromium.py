# ABOUTME: Tests for Chromium Local Storage token decoding and Cookies DB decryption.
# ABOUTME: Builds synthetic LevelDB directories and Cookies databases; no real Slack data is used.
import json
from pathlib import Path

import pytest

from slarc.adapters.chromium import (
    TeamToken,
    decrypt_cookie,
    read_d_cookie,
    read_local_config,
)
from tests.cookies_writer import encrypt, make_cookies_db
from tests.leveldb_writer import write_log

KEY = b"_https://app.slack.com\x00\x01localConfig_v2"
OTHER_KEY = b"_https://app.slack.com\x00\x01other"
COOKIE = "xoxd-abc%2Bdef"
PASSWORD = b"keychain-secret"


def config_json(token: str = "xoxc-1", domain: str = "acme") -> bytes:
    teams = {"T1": {"token": token, "domain": domain, "name": "Acme", "extra": 1}}
    return json.dumps({"teams": teams}).encode()


def test_reads_teams_from_latin1_value(tmp_path: Path):
    write_log(tmp_path / "000003.log", [[(KEY, b"\x01" + config_json())]], 1)
    assert read_local_config(tmp_path) == [TeamToken("T1", "acme", "Acme", "xoxc-1")]


def test_reads_teams_from_utf16_value(tmp_path: Path):
    value = b"\x00" + config_json().decode().encode("utf-16-le")
    write_log(tmp_path / "000003.log", [[(KEY, value), (OTHER_KEY, b"\x01junk")]], 1)
    assert read_local_config(tmp_path) == [TeamToken("T1", "acme", "Acme", "xoxc-1")]


def test_newest_local_config_wins(tmp_path: Path):
    write_log(
        tmp_path / "000003.log",
        [[(KEY, b"\x01" + config_json("xoxc-old"))], [(KEY, b"\x01" + config_json("xoxc-new"))]],
        1,
    )
    assert [t.token for t in read_local_config(tmp_path)] == ["xoxc-new"]


def test_deleted_local_config_yields_no_tokens(tmp_path: Path):
    write_log(tmp_path / "000003.log", [[(KEY, b"\x01" + config_json())], [(KEY, None)]], 1)
    assert read_local_config(tmp_path) == []


def test_tokens_not_in_repr():
    assert "xoxc-secret" not in repr(TeamToken("T1", "acme", "Acme", "xoxc-secret"))


def test_decrypt_v10_macos():
    encrypted = encrypt(COOKIE.encode(), PASSWORD, 1003)
    assert decrypt_cookie(encrypted, PASSWORD, 1003, 23) == COOKIE


def test_decrypt_strips_domain_hash_prefix_for_v24():
    encrypted = encrypt(b"\x07" * 32 + COOKIE.encode(), PASSWORD, 1)
    assert decrypt_cookie(encrypted, PASSWORD, 1, 24) == COOKIE


def test_wrong_password_raises_without_secret_in_message():
    encrypted = encrypt(COOKIE.encode(), PASSWORD, 1003)
    with pytest.raises(ValueError, match="cookie did not decrypt") as exc:
        decrypt_cookie(encrypted, b"wrong", 1003, 23)
    assert COOKIE not in str(exc.value)


def slack_cookie_rows(encrypted: bytes) -> list[tuple[str, str, bytes]]:
    return [(".slack.com", "b", b"v10junk"), (".slack.com", "d", encrypted)]


def test_read_d_cookie_from_sqlite(tmp_path: Path):
    encrypted = encrypt(b"\x07" * 32 + COOKIE.encode(), PASSWORD, 1003)
    make_cookies_db(tmp_path / "Cookies", slack_cookie_rows(encrypted), 24)
    assert read_d_cookie(tmp_path / "Cookies", [b"nope", PASSWORD], 1003) == COOKIE


def test_read_d_cookie_without_matching_password_raises(tmp_path: Path):
    make_cookies_db(
        tmp_path / "Cookies", slack_cookie_rows(encrypt(COOKIE.encode(), PASSWORD, 1003)), 23
    )
    with pytest.raises(ValueError, match="cookie did not decrypt"):
        read_d_cookie(tmp_path / "Cookies", [b"nope"], 1003)


def test_read_d_cookie_matches_slack_host_exactly_preferring_dot_slack_com(tmp_path: Path):
    rows = [
        ("evilslack.com", "d", encrypt(b"xoxd-evil", PASSWORD, 1003)),
        (".notslack.com", "d", encrypt(b"xoxd-not", PASSWORD, 1003)),
        ("slack.com", "d", encrypt(b"xoxd-bare", PASSWORD, 1003)),
        (".slack.com", "d", encrypt(COOKIE.encode(), PASSWORD, 1003)),
    ]
    make_cookies_db(tmp_path / "Cookies", rows, 23)
    assert read_d_cookie(tmp_path / "Cookies", [PASSWORD], 1003) == COOKIE


def test_read_d_cookie_falls_back_to_bare_slack_com(tmp_path: Path):
    rows = [
        ("evilslack.com", "d", encrypt(b"xoxd-evil", PASSWORD, 1003)),
        ("slack.com", "d", encrypt(COOKIE.encode(), PASSWORD, 1003)),
    ]
    make_cookies_db(tmp_path / "Cookies", rows, 23)
    assert read_d_cookie(tmp_path / "Cookies", [PASSWORD], 1003) == COOKIE
