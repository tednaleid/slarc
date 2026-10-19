# ABOUTME: Reads Slack sessions from the Slack desktop app on macOS and Linux.
# ABOUTME: Finds the data directory, copies Local Storage and decrypts the d cookie with the OS key.
import importlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

from slarc.adapters.chromium import (
    DECRYPT_FAILED,
    NO_COOKIE,
    TeamToken,
    read_d_cookie,
    read_local_config,
)
from slarc.ports import CredentialReadError, Session

SOURCE = "desktop"

MACOS_DATA_DIRS = (
    "Library/Application Support/Slack",
    "Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack",
)
LINUX_DATA_DIRS = (
    ".config/Slack",
    ".var/app/com.slack.Slack/config/Slack",
    "snap/slack/current/.config/Slack",
)
LEVELDB_SUBDIR = Path("Local Storage") / "leveldb"
COOKIES_CANDIDATES = (Path("Cookies"), Path("Network") / "Cookies")

SECURITY_COMMAND = "/usr/bin/security"
KEYCHAIN_SERVICE = "Slack Safe Storage"
KEYCHAIN_ACCOUNTS = ("Slack App Store Key", "Slack Key")
MACOS_ITERATIONS = 1003

SECRET_SERVICE_QUERIES = (
    {"xdg:schema": "chrome_libsecret_os_crypt_password_v2", "application": "Slack"},
    {"xdg:schema": "chrome_libsecret_os_crypt_password_v1", "application": "Slack"},
)
LINUX_FIXED_PASSWORD = b"peanuts"
LINUX_ITERATIONS = 1

NO_APP_DATA = "no Slack desktop app data found"
LOCAL_STORAGE_UNREADABLE = "Local Storage could not be read"
NO_TOKENS = "no tokens in Local Storage"
KEYCHAIN_REFUSED = "keychain read refused"
COOKIES_UNREADABLE = "Cookies database could not be read"
COOKIE_STEPS = {
    DECRYPT_FAILED: DECRYPT_FAILED,
    NO_COOKIE: "no d cookie in the Cookies database",
}

READ_ERRORS = (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError, sqlite3.Error)


def data_dir_candidates(home: Path, platform: str, env: Mapping[str, str]) -> list[Path]:
    if platform == "darwin":
        return [home / relative for relative in MACOS_DATA_DIRS]
    if platform.startswith("linux"):
        xdg = [Path(env["XDG_CONFIG_HOME"]) / "Slack"] if env.get("XDG_CONFIG_HOME") else []
        return xdg + [home / relative for relative in LINUX_DATA_DIRS]
    return []


def find_data_dir(home: Path, platform: str, env: Mapping[str, str]) -> Path | None:
    for candidate in data_dir_candidates(home, platform, env):
        if (candidate / LEVELDB_SUBDIR).is_dir():
            return candidate
    return None


def copy_leveldb(source_dir: Path, target_dir: Path) -> None:
    """Copy a LevelDB directory's files, skipping the LOCK a running Slack holds."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in source_dir.iterdir():
        if path.is_file() and path.name != "LOCK":
            shutil.copyfile(path, target_dir / path.name)


def secret_service_passwords() -> list[bytes]:
    """Return the Slack Safe Storage key from the Secret Service, or nothing if unavailable."""
    try:
        secretstorage = importlib.import_module("secretstorage")
    except ImportError:
        return []
    try:
        with closing(secretstorage.dbus_init()) as connection:
            for attributes in SECRET_SERVICE_QUERIES:
                for item in secretstorage.search_items(connection, attributes):
                    if not item.is_locked():
                        return [item.get_secret()]
    except Exception:
        # Any Secret Service failure leaves the fixed Linux password as the only candidate.
        return []
    return []


class DesktopSessionSource:
    source = SOURCE

    def __init__(
        self,
        home: Path,
        platform: str,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        *,
        env: Mapping[str, str] = os.environ,
        secret_lookup: Callable[[], list[bytes]] = secret_service_passwords,
    ) -> None:
        self._home = home
        self._platform = platform
        self._run = run
        self._env = env
        self._secret_lookup = secret_lookup

    def sessions(self) -> list[Session]:
        data_dir = find_data_dir(self._home, self._platform, self._env)
        if data_dir is None:
            raise CredentialReadError(SOURCE, NO_APP_DATA)
        tokens = self._read_tokens(data_dir / LEVELDB_SUBDIR)
        if not tokens:
            raise CredentialReadError(SOURCE, NO_TOKENS)
        xoxd = self._read_cookie(data_dir)
        return [Session(t.team_id, t.domain, t.name, SOURCE, t.token, xoxd) for t in tokens]

    def _read_tokens(self, leveldb_dir: Path) -> list[TeamToken]:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                copy = Path(tmp) / "leveldb"
                copy_leveldb(leveldb_dir, copy)
                tokens = read_local_config(copy)
        except READ_ERRORS:
            raise CredentialReadError(SOURCE, LOCAL_STORAGE_UNREADABLE) from None
        by_team: dict[str, TeamToken] = {}
        for token in tokens:
            by_team.setdefault(token.team_id, token)
        return list(by_team.values())

    def _read_cookie(self, data_dir: Path) -> str:
        if self._platform == "darwin":
            passwords, iterations = self._keychain_passwords(), MACOS_ITERATIONS
        else:
            passwords = [*self._secret_lookup(), LINUX_FIXED_PASSWORD]
            iterations = LINUX_ITERATIONS
        cookies = next(
            (data_dir / c for c in COOKIES_CANDIDATES if (data_dir / c).is_file()),
            data_dir / COOKIES_CANDIDATES[0],
        )
        try:
            return read_d_cookie(cookies, passwords, iterations)
        except ValueError as error:
            step = COOKIE_STEPS.get(str(error), COOKIES_UNREADABLE)
            raise CredentialReadError(SOURCE, step) from None
        except READ_ERRORS:
            raise CredentialReadError(SOURCE, COOKIES_UNREADABLE) from None

    def _keychain_passwords(self) -> list[bytes]:
        passwords: list[bytes] = []
        for account in KEYCHAIN_ACCOUNTS:
            command = [SECURITY_COMMAND, "find-generic-password", "-w"]
            command += ["-s", KEYCHAIN_SERVICE, "-a", account]
            try:
                result = self._run(command, capture_output=True, text=True, check=False)
            except OSError:
                continue
            password = result.stdout.rstrip("\r\n")
            if result.returncode == 0 and password:
                passwords.append(password.encode())
        if not passwords:
            raise CredentialReadError(SOURCE, KEYCHAIN_REFUSED)
        return passwords
