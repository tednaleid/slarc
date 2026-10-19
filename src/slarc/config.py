# ABOUTME: Finds the archive and resolves settings from flags, env, slarc.toml and the global file.
# ABOUTME: Reads and writes config files only; inputs such as cwd, env and home are passed in.
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from slarc.ports import SlarcError

ARCHIVE_FILE = "slarc.toml"
GLOBAL_PATH_KEY = "_path"

DEFAULT_DOWNLOADS = 4
DEFAULT_MAX_FILE_SIZE_MB = 100
DEFAULT_ACTIVE_THREAD_DAYS = 7

NEW_ARCHIVE_HEADER = """\
# slarc archive settings. All keys are optional; see `slarc list` for the files read.
# [pull]
# downloads = 4
# max_file_size_mb = 100
# active_thread_days = 7
#
# [export]
# tz = "America/Chicago"
"""


@dataclass(frozen=True)
class Settings:
    archive: Path
    downloads: int = DEFAULT_DOWNLOADS
    max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB
    active_thread_days: int = DEFAULT_ACTIVE_THREAD_DAYS
    tz: str | None = None
    sources: list[Path] = field(default_factory=list)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SlarcError(f"Could not parse {path}: {e}.") from e


def read_global_config(env: Mapping[str, str], home: Path) -> dict[str, Any]:
    config_home = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    path = config_home / "slarc" / "config.toml"
    if not path.is_file():
        return {}
    return {**_read_toml(path), GLOBAL_PATH_KEY: str(path)}


def find_archive(
    cwd: Path,
    root_flag: Path | None,
    env: Mapping[str, str],
    global_config: Mapping[str, Any],
) -> Path | None:
    if root_flag is not None:
        return root_flag
    if env.get("SLARC_ARCHIVE"):
        return Path(env["SLARC_ARCHIVE"])
    for directory in (cwd, *cwd.parents):
        if (directory / ARCHIVE_FILE).is_file():
            return directory
    default = global_config.get("default_archive")
    if default and GLOBAL_PATH_KEY in global_config:
        base = Path(global_config[GLOBAL_PATH_KEY]).parent
        return (base / Path(default).expanduser()).resolve()
    return None


def _pick(
    key: str,
    section: str,
    flags: Mapping[str, Any],
    env_name: str | None,
    env: Mapping[str, str],
    layers: list[Mapping[str, Any]],
    default: Any,
) -> Any:
    if flags.get(key) is not None:
        return flags[key]
    if env_name and env.get(env_name):
        return type(default)(env[env_name]) if default is not None else env[env_name]
    for layer in layers:
        value = layer.get(section, {}).get(key)
        if value is not None:
            return value
    return default


def load_settings(
    archive: Path,
    global_config: Mapping[str, Any],
    env: Mapping[str, str],
    flags: Mapping[str, Any],
) -> Settings:
    sources: list[Path] = []
    local: dict[str, Any] = {}
    local_path = archive / ARCHIVE_FILE
    if local_path.is_file():
        local = _read_toml(local_path)
        sources.append(local_path)
    if GLOBAL_PATH_KEY in global_config:
        sources.append(Path(global_config[GLOBAL_PATH_KEY]))
    layers = [local, global_config]
    return Settings(
        archive=archive,
        downloads=_pick(
            "downloads", "pull", flags, "SLARC_DOWNLOADS", env, layers, DEFAULT_DOWNLOADS
        ),
        max_file_size_mb=_pick(
            "max_file_size_mb",
            "pull",
            flags,
            "SLARC_MAX_FILE_SIZE_MB",
            env,
            layers,
            DEFAULT_MAX_FILE_SIZE_MB,
        ),
        active_thread_days=_pick(
            "active_thread_days", "pull", flags, None, env, layers, DEFAULT_ACTIVE_THREAD_DAYS
        ),
        tz=_pick("tz", "export", flags, None, env, layers, None),
        sources=sources,
    )


def create_archive(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ARCHIVE_FILE).write_text(NEW_ARCHIVE_HEADER, encoding="utf-8")
