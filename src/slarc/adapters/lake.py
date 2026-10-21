# ABOUTME: Connects to an archive's DuckLake: a SQLite catalog in WAL mode plus Parquet data files.
# ABOUTME: Creates the tables and settings on first connect, manages the DuckDB extensions and
# ABOUTME: keeps the record of the machine that owns the archive.
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from slarc.ports import ArchiveReadFailed, ArchiveWriteFailed, SlarcError

CATALOG = "catalog.sqlite"
CREATING = f"{CATALOG}.creating"  # where a new catalog is built before it is renamed
DATA_DIR = "data"
BLOB_DIR = "blobs"
EXTENSIONS = ("ducklake", "sqlite_scanner")

# DuckDB's threads for each lake connection, or None for one per core. Tests use 1, since starting
# a thread per core for each connection costs more than it saves on a small lake.
DUCKDB_THREADS: int | None = None

# DuckLake attaches the SQLite catalog under this name. The owner record is a table of the catalog
# itself, outside the lake, so it never moves into a data file that has not synced yet.
METADATA = "__ducklake_metadata_lake"
OWNER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {METADATA}.archive_owner (
    machine_id VARCHAR, host_name VARCHAR, since VARCHAR);
"""

# Every file the catalog references, with the table and schema directories its path may be
# relative to; a relative schema path is relative to the data path.
REFERENCED_FILES = f"""
SELECT f.path, f.path_is_relative, t.path, t.path_is_relative, s.path, s.path_is_relative
FROM (
    SELECT table_id, path, path_is_relative FROM {METADATA}.ducklake_data_file
    UNION ALL
    SELECT table_id, path, path_is_relative FROM {METADATA}.ducklake_delete_file
) f
JOIN (
    SELECT DISTINCT table_id, schema_id, path, path_is_relative FROM {METADATA}.ducklake_table
) t ON t.table_id = f.table_id
JOIN (
    SELECT DISTINCT schema_id, path, path_is_relative FROM {METADATA}.ducklake_schema
) s ON s.schema_id = t.schema_id
"""

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


class OwnedElsewhere(SlarcError):
    def __init__(self, host_name: str) -> None:
        super().__init__(
            f"This archive is owned by {host_name}. Wait until your synced folder has finished "
            "syncing, then run the command again with --take-ownership."
        )


class CatalogNotSynced(SlarcError):
    def __init__(self) -> None:
        super().__init__(
            "This archive has data files but no catalog.sqlite, so wait until your synced "
            "folder has finished syncing and run the command again. To start over instead, "
            "delete data/ and blobs/ as well."
        )


class SyncNotFinished(SlarcError):
    def __init__(self) -> None:
        super().__init__(
            "Some of this archive's files have not arrived yet; "
            "wait until your synced folder has finished syncing."
        )


def failure_reason(error: BaseException) -> str:
    """The error's first line without its closing punctuation, to quote inside a sentence."""
    lines = str(error).splitlines() or [type(error).__name__]
    return lines[0].rstrip(".!")


@contextmanager
def lake_errors(
    catalog: Path, failure: type[ArchiveReadFailed] | type[ArchiveWriteFailed]
) -> Generator[None]:
    """Raises failure, naming the catalog, for a DuckDB, SQLite or OS error in the block."""
    try:
        yield
    except (duckdb.Error, sqlite3.Error, OSError) as error:
        raise failure(catalog, failure_reason(error)) from None


def cache_home(env: Mapping[str, str], home: Path) -> Path:
    """$XDG_CACHE_HOME, or ~/.cache when it is unset or empty."""
    return Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache"


def extension_directory(env: Mapping[str, str], home: Path) -> Path:
    return cache_home(env, home) / "slarc" / "duckdb"


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

    A writable connection creates the catalog, tables and options when they are missing, so the
    caller holds the archive's write lock. It raises CatalogNotSynced instead of creating a
    catalog beside data or blob files, which a synced folder may have delivered before the
    catalog. A new catalog is built under a temporary name and
    renamed into place, so a catalog that exists is complete. It raises ArchiveWriteFailed, and
    a read-only one ArchiveReadFailed, when it cannot open the catalog. `autoinstall=False`
    skips the download and exists for tests.
    """
    con = duckdb.connect(":memory:", config={"threads": DUCKDB_THREADS} if DUCKDB_THREADS else {})
    try:
        _load_extensions(con, ext_dir, autoinstall=autoinstall)
        if not read_only:
            root.mkdir(parents=True, exist_ok=True)
            if not (root / CATALOG).exists():
                if _has_files(root / DATA_DIR) or _has_files(root / BLOB_DIR):
                    raise CatalogNotSynced()
                _create_catalog(con, root)
        _attach(con, root, root / CATALOG, read_only=read_only)
        if not read_only:
            _create_missing(con)
    except BaseException:
        con.close()
        raise
    return con


def _has_files(directory: Path) -> bool:
    return any(path.is_file() for path in directory.rglob("*"))


def _create_catalog(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    """Builds an empty lake at CREATING, after deleting any a killed creator left, and renames
    it to CATALOG. Detaching closes the SQLite catalog, which checkpoints and removes its WAL.
    """
    creating = root / CREATING
    for leftover in (creating, *(Path(f"{creating}{suffix}") for suffix in ("-wal", "-shm"))):
        leftover.unlink(missing_ok=True)
    _attach(con, root, creating, read_only=False)
    _create_missing(con)
    con.execute("USE memory")
    con.execute("DETACH lake")
    creating.rename(root / CATALOG)


def _attach(con: duckdb.DuckDBPyConnection, root: Path, catalog: Path, *, read_only: bool) -> None:
    attach_options = [
        f"DATA_PATH {_sql_string(f'{root / DATA_DIR}/')}",
        "OVERRIDE_DATA_PATH true",
        "METADATA_PARAMETERS MAP {'journal_mode': 'wal'}",
    ]
    if read_only:
        attach_options.append("READ_ONLY")
    try:
        target = _sql_string(f"ducklake:sqlite:{catalog}")
        con.execute(f"ATTACH {target} AS lake ({', '.join(attach_options)})")
    except duckdb.Error as error:
        # DuckDB's message ends with the cause, after the catalog path it already names.
        reason = str(error).splitlines()[0].rsplit(": ", 1)[-1]
        failure = ArchiveReadFailed if read_only else ArchiveWriteFailed
        raise failure(root / CATALOG, reason) from None
    con.execute("USE lake")


def _sql_string(text: str) -> str:
    """A SQL string literal of text, for statements such as ATTACH that take no parameters."""
    return "'" + text.replace("'", "''") + "'"


def _create_missing(con: duckdb.DuckDBPyConnection) -> None:
    """Creates missing tables in one transaction, so a new lake gets one snapshot for them."""
    con.execute("BEGIN")
    con.execute(SCHEMA)
    con.execute("COMMIT")
    con.execute(OWNER_SCHEMA)
    present = {row[0] for row in con.execute("SELECT option_name FROM lake.options()").fetchall()}
    for name, value in OPTIONS.items():
        if name not in present:
            con.execute("CALL lake.set_option(?, ?)", [name, value])


def check_owner(
    con: duckdb.DuckDBPyConnection, machine: str, host: str, take_ownership: bool
) -> bool:
    """Checks the archive's owner record against this machine; returns whether the run is
    cautious, which it is only when it takes ownership.

    With no record it records this machine, and with one naming this machine it does nothing.
    One naming another machine raises OwnedElsewhere unless take_ownership is set. Taking
    ownership raises SyncNotFinished, writing nothing, when a file the catalog references is
    missing, and otherwise records this machine. Only a recording needs a writable connection,
    and the caller holds the archive's write lock for it.
    """
    if take_ownership:
        if _missing_files(con):
            raise SyncNotFinished()
        _record_owner(con, machine, host)
        return True
    owner = read_owner(con)
    if owner is None:
        _record_owner(con, machine, host)
    elif owner[0] != machine:
        raise OwnedElsewhere(owner[1])
    return False


def read_owner(con: duckdb.DuckDBPyConnection) -> tuple[str, str] | None:
    """The machine id and host name the owner record names, or None when it names none."""
    try:
        rows = con.execute(f"SELECT machine_id, host_name FROM {METADATA}.archive_owner").fetchall()
    except duckdb.CatalogException:
        return None  # a catalog without the owner table has no owner yet
    return rows[0] if rows else None


def _record_owner(con: duckdb.DuckDBPyConnection, machine: str, host: str) -> None:
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("BEGIN")
    try:
        con.execute(f"DELETE FROM {METADATA}.archive_owner")
        con.execute(
            f"INSERT INTO {METADATA}.archive_owner VALUES (?, ?, ?)", [machine, host, since]
        )
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _missing_files(con: duckdb.DuckDBPyConnection) -> bool:
    data_dir = _data_dir(con)
    for file, file_rel, table, table_rel, schema, schema_rel in con.execute(
        REFERENCED_FILES
    ).fetchall():
        schema_dir = _resolve(data_dir, schema, schema_rel)
        if not _resolve(_resolve(schema_dir, table, table_rel), file, file_rel).exists():
            return True
    return False


def _data_dir(con: duckdb.DuckDBPyConnection) -> Path:
    """The data path connect_lake attached: DATA_DIR beside the catalog."""
    rows = con.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = 'lake'"
    ).fetchall()
    return Path(rows[0][0].removeprefix("sqlite:")).parent / DATA_DIR


def _resolve(base: Path, path: str, relative: bool) -> Path:
    return base / path if relative else Path(path)
