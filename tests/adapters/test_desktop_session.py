# ABOUTME: Tests for reading Slack sessions from a synthetic Slack desktop app data directory.
# ABOUTME: Uses a fake home tree and a fake keychain command; the real keychain is never touched.
import json
import subprocess
from pathlib import Path

import pytest

from slarc.adapters.desktop_session import DesktopSessionSource
from slarc.ports import CredentialReadError, Session
from tests.cookies_writer import encrypt, make_cookies_db
from tests.leveldb_writer import write_log

APP_STORE_DIR = (
    "Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack"
)
UNSANDBOXED_DIR = "Library/Application Support/Slack"
CONFIG_KEY = b"\x00\x01localConfig_v2"
COOKIE = "xoxd-abc%2Bdef"
PASSWORD = "keychain-secret"


def write_slack_dir(
    data_dir: Path,
    teams: dict[str, dict],
    password: bytes,
    iterations: int,
    origins: tuple[str, ...] = ("https://app.slack.com",),
) -> None:
    leveldb_dir = data_dir / "Local Storage" / "leveldb"
    leveldb_dir.mkdir(parents=True)
    value = b"\x01" + json.dumps({"teams": teams}).encode()
    batch: list[tuple[bytes, bytes | None]] = [
        (b"_" + origin.encode() + CONFIG_KEY, value) for origin in origins
    ]
    write_log(leveldb_dir / "000003.log", [batch], 1)
    (leveldb_dir / "LOCK").write_bytes(b"")
    encrypted = encrypt(b"\x07" * 32 + COOKIE.encode(), password, iterations)
    make_cookies_db(data_dir / "Cookies", [(".slack.com", "d", encrypted)], 24)


def team(token: str, domain: str, name: str) -> dict:
    return {"token": token, "domain": domain, "name": name}


class FakeRun:
    """Stands in for subprocess.run, answering keychain reads for the given accounts."""

    def __init__(self, passwords: dict[str, str]):
        self.passwords = passwords
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        account = args[args.index("-a") + 1]
        if account in self.passwords:
            return subprocess.CompletedProcess(args, 0, self.passwords[account] + "\n", "")
        return subprocess.CompletedProcess(args, 44, "", "item not found")


def test_desktop_source_prefers_unsandboxed_dir(tmp_path: Path):
    write_slack_dir(tmp_path / UNSANDBOXED_DIR, {"T1": team("xoxc-1", "one", "One")}, b"pw", 1003)
    write_slack_dir(tmp_path / APP_STORE_DIR, {"T2": team("xoxc-2", "two", "Two")}, b"pw", 1003)
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({"Slack Key": "pw"}))
    assert [s.team_id for s in source.sessions()] == ["T1"]


def test_desktop_source_uses_app_store_dir(tmp_path: Path):
    teams = {"T1": team("xoxc-1", "one", "One"), "T2": team("xoxc-2", "two", "Two")}
    write_slack_dir(tmp_path / APP_STORE_DIR, teams, PASSWORD.encode(), 1003)
    run = FakeRun({"Slack App Store Key": PASSWORD})
    sessions = DesktopSessionSource(tmp_path, "darwin", run=run).sessions()
    assert sessions == [
        Session("T1", "one", "One", "desktop", "xoxc-1", COOKIE),
        Session("T2", "two", "Two", "desktop", "xoxc-2", COOKIE),
    ]
    assert run.calls[0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-w",
        "-s",
        "Slack Safe Storage",
        "-a",
        "Slack App Store Key",
    ]


def test_desktop_source_tries_slack_key_after_app_store_key(tmp_path: Path):
    write_slack_dir(tmp_path / APP_STORE_DIR, {"T1": team("xoxc-1", "one", "One")}, b"pw", 1003)
    run = FakeRun({"Slack App Store Key": "stale", "Slack Key": "pw"})
    sessions = DesktopSessionSource(tmp_path, "darwin", run=run).sessions()
    assert [s.xoxd for s in sessions] == [COOKIE]


def test_desktop_source_keeps_one_session_per_team(tmp_path: Path):
    write_slack_dir(
        tmp_path / APP_STORE_DIR,
        {"T1": team("xoxc-1", "one", "One")},
        b"pw",
        1003,
        origins=("https://app.slack.com", "https://one.slack.com"),
    )
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({"Slack App Store Key": "pw"}))
    assert [s.team_id for s in source.sessions()] == ["T1"]


def test_desktop_source_reports_missing_app_cleanly(tmp_path: Path):
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({}))
    with pytest.raises(CredentialReadError) as exc:
        source.sessions()
    assert exc.value.source == "desktop"
    assert exc.value.step == "no Slack desktop app data found"
    assert "no Slack desktop app data found" in str(exc.value)
    assert "slarc auth --paste" in str(exc.value)


def test_keychain_refusal_names_step(tmp_path: Path):
    write_slack_dir(tmp_path / APP_STORE_DIR, {"T1": team("xoxc-1", "one", "One")}, b"pw", 1003)
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({}))
    with pytest.raises(CredentialReadError) as exc:
        source.sessions()
    assert exc.value.step == "keychain read refused"
    assert "slarc auth --paste" in str(exc.value)


def test_wrong_keychain_password_names_decrypt_step(tmp_path: Path):
    write_slack_dir(tmp_path / APP_STORE_DIR, {"T1": team("xoxc-1", "one", "One")}, b"pw", 1003)
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({"Slack Key": "wrong"}))
    with pytest.raises(CredentialReadError) as exc:
        source.sessions()
    assert exc.value.step == "cookie did not decrypt"
    assert "wrong" not in str(exc.value)


def test_empty_local_storage_names_step(tmp_path: Path):
    write_slack_dir(tmp_path / APP_STORE_DIR, {}, b"pw", 1003)
    run = FakeRun({"Slack App Store Key": "pw"})
    with pytest.raises(CredentialReadError) as exc:
        DesktopSessionSource(tmp_path, "darwin", run=run).sessions()
    assert exc.value.step == "no tokens in Local Storage"
    assert run.calls == []


def test_unreadable_local_storage_does_not_echo_contents(tmp_path: Path):
    data_dir = tmp_path / APP_STORE_DIR
    write_slack_dir(data_dir, {"T1": team("xoxc-secret", "one", "One")}, b"pw", 1003)
    leveldb_dir = data_dir / "Local Storage" / "leveldb"
    value = b"\x01" + b'{"teams": {"T1": {"token": "xoxc-secret"'
    write_log(leveldb_dir / "000003.log", [[(b"_https://app.slack.com" + CONFIG_KEY, value)]], 1)
    source = DesktopSessionSource(tmp_path, "darwin", run=FakeRun({"Slack App Store Key": "pw"}))
    with pytest.raises(CredentialReadError) as exc:
        source.sessions()
    assert exc.value.step == "Local Storage could not be read"
    assert exc.value.__cause__ is None
    assert "xoxc-secret" not in str(exc.value)


def test_linux_source_uses_secret_service_then_peanuts(tmp_path: Path):
    teams = {"T1": team("xoxc-1", "one", "One")}
    write_slack_dir(tmp_path / ".config/Slack", teams, b"peanuts", 1)
    source = DesktopSessionSource(
        tmp_path, "linux", run=FakeRun({}), env={}, secret_lookup=lambda: [b"stale"]
    )
    assert [s.xoxd for s in source.sessions()] == [COOKIE]


def test_linux_source_prefers_xdg_config_home(tmp_path: Path):
    write_slack_dir(tmp_path / "xdg/Slack", {"T1": team("xoxc-1", "one", "One")}, b"pw", 1)
    write_slack_dir(tmp_path / ".config/Slack", {"T2": team("xoxc-2", "two", "Two")}, b"pw", 1)
    source = DesktopSessionSource(
        tmp_path,
        "linux",
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        secret_lookup=lambda: [b"pw"],
    )
    assert [s.team_id for s in source.sessions()] == ["T1"]


def test_linux_source_finds_flatpak_dir(tmp_path: Path):
    teams = {"T1": team("xoxc-1", "one", "One")}
    write_slack_dir(tmp_path / ".var/app/com.slack.Slack/config/Slack", teams, b"pw", 1)
    source = DesktopSessionSource(tmp_path, "linux", env={}, secret_lookup=lambda: [b"pw"])
    assert [s.team_id for s in source.sessions()] == ["T1"]
