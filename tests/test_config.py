# ABOUTME: Tests for archive lookup and settings resolution in slarc.config.
# ABOUTME: All filesystem and environment inputs are temp dirs and explicit mappings.
import tomllib
from pathlib import Path

import pytest

from slarc.config import (
    Settings,
    create_archive,
    find_archive,
    load_settings,
    read_global_config,
)
from slarc.ports import SlarcError


def make_archive(path: Path, body: str = "") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "slarc.toml").write_text(body)
    return path


def test_finds_slarc_toml_in_parent(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a")
    deep = archive / "x" / "y"
    deep.mkdir(parents=True)
    assert find_archive(deep, None, {}, {}) == archive


def test_root_flag_wins(tmp_path: Path) -> None:
    local = make_archive(tmp_path / "local")
    flag = make_archive(tmp_path / "flag")
    env_dir = make_archive(tmp_path / "env")
    assert find_archive(local, flag, {"SLARC_ARCHIVE": str(env_dir)}, {}) == flag


def test_env_beats_walk_up(tmp_path: Path) -> None:
    local = make_archive(tmp_path / "local")
    env_dir = make_archive(tmp_path / "env")
    assert find_archive(local, None, {"SLARC_ARCHIVE": str(env_dir)}, {}) == env_dir


def test_global_default_archive_used_when_no_local(tmp_path: Path) -> None:
    default = make_archive(tmp_path / "home" / "archive")
    cfg_dir = tmp_path / "xdg" / "slarc"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('default_archive = "../../home/archive"\n')
    global_config = read_global_config(
        {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, tmp_path / "nohome"
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        find_archive(empty, None, {}, global_config) == (cfg_dir / "../../home/archive").resolve()
    )
    assert default.resolve() == (cfg_dir / "../../home/archive").resolve()


def test_global_default_archive_expands_tilde(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_archive(home / "arc")
    cfg_dir = home / ".config" / "slarc"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('default_archive = "~/arc"\n')
    global_config = read_global_config({"HOME": str(home)}, home)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_archive(empty, None, {}, global_config) is not None


def test_returns_none_when_nothing_found(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_archive(empty, None, {}, {}) is None


def test_settings_precedence_flag_env_local_global_default(tmp_path: Path) -> None:
    archive = make_archive(
        tmp_path / "a",
        "[pull]\ndownloads = 3\nmax_file_size_mb = 30\n"
        'active_thread_days = 3\n[export]\ntz = "UTC"\n',
    )
    global_config = {
        "pull": {"downloads": 2, "max_file_size_mb": 20, "active_thread_days": 2, "extra": 1},
        "export": {"tz": "Asia/Tokyo"},
    }
    env = {"SLARC_DOWNLOADS": "5"}

    s = load_settings(archive, global_config, env, {"downloads": 6})
    assert s.downloads == 6
    assert s.max_file_size_mb == 30
    assert s.active_thread_days == 3
    assert s.tz == "UTC"

    s = load_settings(archive, global_config, env, {})
    assert s.downloads == 5

    s = load_settings(archive, global_config, {"SLARC_MAX_FILE_SIZE_MB": "7"}, {"downloads": None})
    assert s.downloads == 3
    assert s.max_file_size_mb == 7

    bare = make_archive(tmp_path / "b")
    s = load_settings(bare, global_config, {}, {})
    assert (s.downloads, s.max_file_size_mb, s.active_thread_days, s.tz) == (2, 20, 2, "Asia/Tokyo")

    s = load_settings(bare, {}, {}, {})
    assert (s.downloads, s.max_file_size_mb, s.active_thread_days, s.tz) == (4, 100, 7, None)


def test_create_archive_writes_slarc_toml(tmp_path: Path) -> None:
    target = tmp_path / "new"
    create_archive(target)
    text = (target / "slarc.toml").read_text()
    assert text.startswith("#")
    assert tomllib.loads(text) == {}


def test_sources_lists_files_read(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a")
    cfg_dir = tmp_path / "xdg" / "slarc"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("[pull]\ndownloads = 2\n")
    global_config = read_global_config({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, tmp_path)
    s = load_settings(archive, global_config, {}, {})
    assert s.sources == [archive / "slarc.toml", cfg_dir / "config.toml"]
    assert s.archive == archive
    assert isinstance(s, Settings)

    s = load_settings(tmp_path / "missing", {}, {}, {})
    assert s.sources == []


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a", "[pull\n")
    with pytest.raises(SlarcError, match=r"slarc\.toml"):
        load_settings(archive, {}, {}, {})


def test_read_global_config_default_location(tmp_path: Path) -> None:
    assert read_global_config({}, tmp_path) == {}
    cfg_dir = tmp_path / ".config" / "slarc"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text("[pull]\ndownloads = 9\n")
    assert read_global_config({}, tmp_path)["pull"] == {"downloads": 9}


def test_env_value_not_an_integer_names_the_variable(tmp_path: Path) -> None:
    with pytest.raises(SlarcError, match=r"SLARC_DOWNLOADS must be an integer, got 'abc'"):
        load_settings(make_archive(tmp_path / "a"), {}, {"SLARC_DOWNLOADS": "abc"}, {})


def test_toml_value_of_wrong_type_names_file_and_key(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a", '[pull]\ndownloads = "x"\n')
    with pytest.raises(SlarcError, match=r"slarc\.toml.*pull\.downloads"):
        load_settings(archive, {}, {}, {})
    with pytest.raises(SlarcError, match=r"global\.toml.*pull\.downloads"):
        load_settings(
            make_archive(tmp_path / "b"),
            {"_path": str(tmp_path / "global.toml"), "pull": {"downloads": "x"}},
            {},
            {},
        )


def test_toml_table_that_is_not_a_table_names_file(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a", "pull = 3\n")
    with pytest.raises(SlarcError, match=r"slarc\.toml.*\[pull\]"):
        load_settings(archive, {}, {}, {})


def test_create_archive_keeps_existing_slarc_toml(tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "a", "[pull]\ndownloads = 2\n")
    create_archive(archive)
    assert (archive / "slarc.toml").read_text() == "[pull]\ndownloads = 2\n"
