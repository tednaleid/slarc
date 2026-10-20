# ABOUTME: Connects to an archive's DuckLake: a SQLite catalog in WAL mode plus Parquet data files.
# ABOUTME: Creates the tables and settings on first connect and manages the DuckDB extensions.
from collections.abc import Mapping
from pathlib import Path

import duckdb

from slarc.ports import SlarcError

CATALOG = "catalog.sqlite"
DATA_DIR = "data"
EXTENSIONS = ("ducklake", "sqlite_scanner")

# Slarc's own settings for every lake; the reasons are in the lake design's settings section.
OPTIONS = {"target_file_size": "16MB", "rewrite_delete_threshold": "0"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_version (
    team VARCHAR, channel VARCHAR, ts VARCHAR, thread_ts VARCHAR, "user" VARCHAR,
    kind VARCHAR, fetched_at VARCHAR, run VARCHAR, source_team VARCHAR, api VARCHAR,
    slarc VARCHAR, hash VARCHAR, plain VARCHAR, object JSON);
CREATE TABLE IF NOT EXISTS directory_entry (
    team VARCHAR, kind VARCHAR, id VARCHAR, fetched_at VARCHAR, run VARCHAR, api VARCHAR,
    slarc VARCHAR, hash VARCHAR, blob VARCHAR, object JSON);
CREATE TABLE IF NOT EXISTS run (
    run VARCHAR, command VARCHAR, started VARCHAR, finished VARCHAR, fetched_at VARCHAR,
    slarc VARCHAR, object JSON);
CREATE TABLE IF NOT EXISTS tracked_item (
    team VARCHAR, kind VARCHAR, channel VARCHAR, ts VARCHAR, revision INTEGER,
    since VARCHAR, until VARCHAR, newest_ts VARCHAR, oldest_ts VARCHAR,
    backfill_complete BOOLEAN, last_search_at VARCHAR, last_pull_at VARCHAR,
    status VARCHAR, archived BOOLEAN, last_read JSON, retry_threads JSON);
CREATE TABLE IF NOT EXISTS download_state (
    team VARCHAR, target_kind VARCHAR, target VARCHAR, url VARCHAR, status VARCHAR,
    attempts INTEGER, last_error VARCHAR);
"""


class ExtensionsMissing(SlarcError):
    def __init__(self, reason: str) -> None:
        super().__init__(
            "slarc needs the DuckDB extensions ducklake and sqlite_scanner and could not "
            f"install them ({reason}). Run any slarc command once while online to install them."
        )


def extension_directory(env: Mapping[str, str], home: Path) -> Path:
    cache_home = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"
    return cache_home / "slarc" / "duckdb"


def _load_extensions(con: duckdb.DuckDBPyConnection, ext_dir: Path, *, autoinstall: bool) -> None:
    ext_dir.mkdir(parents=True, exist_ok=True)
    con.execute("SET extension_directory = ?", [str(ext_dir)])
    if not autoinstall:
        con.execute("SET autoinstall_known_extensions = false")
        con.execute("SET autoload_known_extensions = false")
    try:
        for name in EXTENSIONS:
            if autoinstall:
                con.execute(f"INSTALL {name}")
            con.execute(f"LOAD {name}")
    except duckdb.Error as e:
        raise ExtensionsMissing(str(e).splitlines()[0]) from None


def install_extensions(ext_dir: Path) -> None:
    con = duckdb.connect(":memory:")
    try:
        _load_extensions(con, ext_dir, autoinstall=True)
    finally:
        con.close()


def connect_lake(
    root: Path,
    ext_dir: Path,
    *,
    read_only: bool = False,
    autoinstall: bool = True,
) -> duckdb.DuckDBPyConnection:
    """Attach the archive's lake as `lake` and make it the default catalog.

    A writable connection creates the catalog, tables and options when they are missing.
    `autoinstall=False` skips the download and exists for tests.
    """
    con = duckdb.connect(":memory:")
    try:
        _load_extensions(con, ext_dir, autoinstall=autoinstall)
        if not read_only:
            root.mkdir(parents=True, exist_ok=True)
        attach_options = [
            f"DATA_PATH '{root / DATA_DIR}/'",
            "OVERRIDE_DATA_PATH true",
            "METADATA_PARAMETERS MAP {'journal_mode': 'wal'}",
        ]
        if read_only:
            attach_options.append("READ_ONLY")
        con.execute(
            f"ATTACH 'ducklake:sqlite:{root / CATALOG}' AS lake ({', '.join(attach_options)})"
        )
        con.execute("USE lake")
        if not read_only:
            _create_missing(con)
    except BaseException:
        con.close()
        raise
    return con


def _create_missing(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)
    present = {row[0] for row in con.execute("SELECT option_name FROM lake.options()").fetchall()}
    for name, value in OPTIONS.items():
        if name not in present:
            con.execute("CALL lake.set_option(?, ?)", [name, value])
