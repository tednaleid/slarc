# ABOUTME: Imports a JSONL archive (raw/, directory/, runs.jsonl) into a new lake, building it under
# ABOUTME: temporary names and verifying it before renaming it into place; also exports a lake back.
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb
import msgspec

from slarc import archive_state
from slarc.adapters.lake import CATALOG, DATA_DIR, connect_lake
from slarc.adapters.lake_store import (
    DIRECTORY_COLUMNS,
    DOWNLOAD_COLUMNS,
    MESSAGE_COLUMNS,
    MESSAGE_KINDS,
    RUN_COLUMNS,
    TRACKED_COLUMNS,
    VERSION_ORDER,
    DuckLakeArchiveStore,
    directory_envelope,
    directory_row,
    download_row,
    download_state,
    insert_rows,
    message_envelope,
    message_row,
    run_line,
    run_row,
    tracked_item,
    tracked_row,
)
from slarc.domain.envelope import Envelope, Kind, decode_line, encode_line, month_of
from slarc.domain.tracking import DownloadState, TrackedItem
from slarc.ports import SlarcError

DIRECTORY_FILES: dict[Kind, str] = {
    "user": "users.jsonl",
    "emoji": "emoji.jsonl",
    "conversation": "conversations.jsonl",
    "bot": "bots.jsonl",
    "usergroup": "usergroups.jsonl",
    "file": "files.jsonl",
    "image": "images.jsonl",
}
RUNS_FILE = "runs.jsonl"
IMPORTING = ".importing"
IMPORTED_REVISION = 1


@dataclass(frozen=True)
class ImportReport:
    """The rows the import wrote per table, and the duplicate message lines it dropped.

    tracked counts the tracked items that are not removed, and removed the ones that are.
    """

    messages: int
    directory: int
    runs: int
    tracked: int
    removed: int
    downloads: int
    duplicates: int


class ImportMismatch(SlarcError):
    def __init__(self, what: str) -> None:
        super().__init__(f"Importing this archive failed its check: {what} did not match.")


@dataclass(frozen=True)
class _JsonlArchive:
    """The rows the import writes, read from an archive's JSONL files."""

    messages: list[Envelope]
    duplicates: int
    directory: list[Envelope]
    runs: list[Envelope]
    tracked: list[TrackedItem]
    downloads: list[DownloadState]


def has_jsonl(root: Path) -> bool:
    return any((root / name).exists() for name in ("raw", "directory", RUNS_FILE))


def import_jsonl(root: Path, ext_dir: Path) -> ImportReport:
    """Builds the archive's lake from its JSONL files, which it leaves untouched.

    The lake is built under temporary names, replacing any an interrupted import left, and
    takes its real names only once verified, the catalog last. A mismatch raises
    ImportMismatch and leaves the temporary names for the next import to replace.
    """
    if (root / CATALOG).exists():
        raise SlarcError(f"This archive already has {CATALOG}, so there is nothing to import.")
    catalog = root / (CATALOG + IMPORTING)
    data = root / (DATA_DIR + IMPORTING)
    # Without a catalog, a data directory is left from a kill between the two renames.
    for path in (catalog, *_sidecars(catalog), data, root / DATA_DIR):
        _remove(path)
    archive = _read_archive(root)
    con = connect_lake(root, ext_dir, catalog=catalog.name, data_dir=data.name)
    try:
        _write(con, archive)
        _verify_counts(con, archive)
    finally:
        con.close()
    _verify_state(
        DuckLakeArchiveStore(root, ext_dir, catalog=catalog.name, data_dir=data.name), archive
    )
    data.rename(root / DATA_DIR)
    catalog.rename(root / CATALOG)
    return ImportReport(
        messages=len(archive.messages),
        directory=len(archive.directory),
        runs=len(archive.runs),
        tracked=sum(1 for item in archive.tracked if item.status != "removed"),
        removed=sum(1 for item in archive.tracked if item.status == "removed"),
        downloads=len(archive.downloads),
        duplicates=archive.duplicates,
    )


def export_jsonl(root: Path, ext_dir: Path, target: Path) -> None:
    """Writes the lake at root to target in the JSONL layout, one run line per run row.

    The last run line carries the current tracked items and download states. A target that an
    earlier run line names but that has no download state now is written there as removed, so
    importing the export reproduces the lake's state.
    """
    con = connect_lake(root, ext_dir, read_only=True)
    try:
        messages = _select(
            con, "message_version", MESSAGE_COLUMNS, f"team, channel, ts, {VERSION_ORDER}"
        )
        entries = _select(con, "directory_entry", DIRECTORY_COLUMNS, f"team, kind, {VERSION_ORDER}")
        runs = _select(con, "run", RUN_COLUMNS, VERSION_ORDER)
        items = _select(con, "tracked_item", TRACKED_COLUMNS, "team, kind, channel, ts")
        states = _select(con, "download_state", DOWNLOAD_COLUMNS, "team, target")
    finally:
        con.close()
    files: dict[Path, list[Envelope]] = {}
    for env in map(message_envelope, messages):
        meta = env.meta
        month = f"{month_of(meta.ts or '0')}.jsonl"
        files.setdefault(target / "raw" / str(meta.team) / str(meta.channel) / month, []).append(
            env
        )
    for env in map(directory_envelope, entries):
        name = DIRECTORY_FILES[env.meta.kind]
        files.setdefault(target / "directory" / str(env.meta.team) / name, []).append(env)
    if runs:
        lines = [run_line(row) for row in runs]
        current = _current_run_line(
            lines, [tracked_item(row) for row in items], [download_state(row) for row in states]
        )
        files[target / RUNS_FILE] = [*lines[:-1], current]
    for path, envs in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode_line(env) for env in envs))


def _read_archive(root: Path) -> _JsonlArchive:
    messages, duplicates = _without_duplicates(
        env
        for env in read_envelopes(sorted(root.glob("raw/*/*/*.jsonl")))
        if env.meta.kind in MESSAGE_KINDS
    )
    directory = list(
        read_envelopes(
            team / name
            for team in sorted(root.glob("directory/*"))
            for name in DIRECTORY_FILES.values()
        )
    )
    runs = [env for env in read_envelopes([root / RUNS_FILE]) if env.meta.kind == "run"]
    fold = archive_state.latest_runs(runs)
    return _JsonlArchive(
        messages=messages,
        duplicates=duplicates,
        directory=directory,
        runs=runs,
        tracked=[
            msgspec.structs.replace(item, revision=IMPORTED_REVISION)
            for item in [*fold.tracked_items().values(), *fold.removed_items().values()]
        ],
        downloads=_outstanding(fold.downloads().values(), directory),
    )


def read_envelopes(paths: Iterable[Path]) -> Iterator[Envelope]:
    """Envelopes from each existing file; blank and undecodable lines are skipped."""
    for path in paths:
        if not path.exists():
            continue
        with path.open("rb") as lines:
            for line in lines:
                if not line.strip():
                    continue
                try:
                    yield decode_line(line)
                except msgspec.DecodeError:
                    continue


def _without_duplicates(lines: Iterable[Envelope]) -> tuple[list[Envelope], int]:
    """The lines without exact repeats, in order, and how many repeats were dropped."""
    kept: dict[tuple, Envelope] = {}
    read = 0
    for env in lines:
        meta = env.meta
        key = (meta.team, meta.channel, meta.ts, meta.fetched_at, meta.run, meta.kind, meta.hash)
        kept.setdefault(key, env)
        read += 1
    return list(kept.values()), read - len(kept)


def _outstanding(states: Iterable[DownloadState], directory: list[Envelope]) -> list[DownloadState]:
    """The states that outstanding_downloads keeps, without removed states.

    That is each state whose target's latest directory line has no blob.
    """
    lines: dict[tuple[str | None, str], list[Envelope]] = {}
    for env in directory:
        lines.setdefault((env.meta.team, env.meta.kind), []).append(env)
    held = {key: archive_state.directory_state(group) for key, group in lines.items()}
    outstanding = []
    for state in states:
        line = held.get((state.team_id, state.target_kind), {}).get(state.target)
        if state.status != "removed" and (line is None or line.blob is None):
            outstanding.append(state)
    return outstanding


def _write(con: duckdb.DuckDBPyConnection, archive: _JsonlArchive) -> None:
    """Writes every row in one commit, sorted as the lake keeps them, ties in file order."""
    messages = sorted(
        archive.messages,
        key=lambda env: (env.meta.team or "", env.meta.channel or "", env.meta.ts or ""),
    )
    entries = sorted(
        archive.directory, key=lambda env: (env.meta.team or "", env.meta.kind, env.meta.id or "")
    )
    con.execute("BEGIN")
    insert_rows(con, "message_version", MESSAGE_COLUMNS, [message_row(env) for env in messages])
    insert_rows(con, "directory_entry", DIRECTORY_COLUMNS, [directory_row(env) for env in entries])
    insert_rows(con, "run", RUN_COLUMNS, [run_row(env) for env in archive.runs])
    insert_rows(
        con, "tracked_item", TRACKED_COLUMNS, [tracked_row(item) for item in archive.tracked]
    )
    insert_rows(
        con, "download_state", DOWNLOAD_COLUMNS, [download_row(s) for s in archive.downloads]
    )
    con.execute("COMMIT")


def _verify_counts(con: duckdb.DuckDBPyConnection, archive: _JsonlArchive) -> None:
    expected = {
        "message_version": len(archive.messages),
        "directory_entry": len(archive.directory),
        "run": len(archive.runs),
        "tracked_item": len(archive.tracked),
        "download_state": len(archive.downloads),
    }
    for table, count in expected.items():
        if con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0] != count:
            raise ImportMismatch(f"the number of {table} rows")


def _verify_state(store: DuckLakeArchiveStore, archive: _JsonlArchive) -> None:
    """The lake's tracked items and download states must equal the ones read from JSONL."""
    tracked = {item.key: item for item in archive.tracked}
    try:
        if store.tracked_items() != {k: v for k, v in tracked.items() if v.status != "removed"}:
            raise ImportMismatch("the tracked items")
        if store.removed_items() != {k: v for k, v in tracked.items() if v.status == "removed"}:
            raise ImportMismatch("the removed items")
        if store.downloads() != {(s.team_id, s.target): s for s in archive.downloads}:
            raise ImportMismatch("the download states")
    finally:
        store.close()


def _select(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str], order_by: str
) -> list[dict]:
    names = ", ".join(f'"{name}"' for name in columns)
    rows = con.execute(f"SELECT {names} FROM {table} ORDER BY {order_by}").fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _current_run_line(
    runs: list[Envelope], tracked: list[TrackedItem], downloads: list[DownloadState]
) -> Envelope:
    """The last run line, carrying the current tracked items and download states."""
    current = {(state.team_id, state.target) for state in downloads}
    earlier = archive_state.latest_runs(runs[:-1]).downloads()
    settled = [
        msgspec.structs.replace(state, status="removed")
        for key, state in earlier.items()
        if key not in current
    ]
    last = runs[-1]
    obj = dict(last.object or {})
    obj["tracked"] = msgspec.to_builtins(tracked)
    obj["downloads"] = msgspec.to_builtins([*downloads, *settled])
    return Envelope(meta=last.meta, object=obj)


def _sidecars(catalog: Path) -> list[Path]:
    return [catalog.with_name(catalog.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
