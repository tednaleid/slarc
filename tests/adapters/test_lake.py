# ABOUTME: Tests for the lake adapter: catalog creation, settings, moved archives and extensions.
# ABOUTME: Connecting downloads the DuckDB extensions once into the real cache directory.
import sqlite3
from pathlib import Path

import duckdb
import pytest

from slarc.adapters.lake import (
    CATALOG,
    ExtensionsMissing,
    connect_lake,
    extension_directory,
)

TABLES = {"message_version", "directory_entry", "run", "tracked_item", "download_state"}


def test_extension_directory_follows_xdg_cache_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert extension_directory({"XDG_CACHE_HOME": str(tmp_path / "c")}, home) == (
        tmp_path / "c" / "slarc" / "duckdb"
    )
    assert extension_directory({}, home) == home / ".cache" / "slarc" / "duckdb"
    assert extension_directory({"XDG_CACHE_HOME": ""}, home) == home / ".cache" / "slarc" / "duckdb"


def test_connect_creates_catalog_tables_and_settings(tmp_path: Path, ext_dir: Path) -> None:
    con = connect_lake(tmp_path, ext_dir)
    options = dict(con.execute("SELECT option_name, value FROM lake.options()").fetchall())
    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    con.close()
    assert options["target_file_size"] == "16000000"  # 16MB, normalized to bytes
    assert float(options["rewrite_delete_threshold"]) == 0
    assert tables >= TABLES
    with sqlite3.connect(tmp_path / CATALOG) as catalog:
        assert catalog.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_moved_archive_still_opens(tmp_path: Path, ext_dir: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    con = connect_lake(old, ext_dir)
    con.execute("INSERT INTO run (run, command) VALUES ('r1', 'pull')")
    con.close()
    new = tmp_path / "new"
    old.rename(new)
    con = connect_lake(new, ext_dir)
    rows = con.execute("SELECT run, command FROM run").fetchall()
    con.close()
    assert rows == [("r1", "pull")]


def test_missing_extensions_raise_a_clear_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ExtensionsMissing) as error:
        connect_lake(tmp_path / "archive", empty, autoinstall=False)
    assert "ducklake" in str(error.value)
    assert "Run any slarc command once while online" in str(error.value)


def test_read_only_connect_cannot_write(tmp_path: Path, ext_dir: Path) -> None:
    connect_lake(tmp_path, ext_dir).close()
    con = connect_lake(tmp_path, ext_dir, read_only=True)
    with pytest.raises(duckdb.Error):
        con.execute("INSERT INTO run (run, command) VALUES ('r1', 'pull')")
    con.close()
