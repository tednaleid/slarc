# ABOUTME: Tests for the lake store's maintenance: compaction after every run, and physical
# ABOUTME: deletion of old files, orphans and unreferenced blobs only while the archive is idle.
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import msgspec
import pytest

from slarc.adapters.blob_store import FsBlobStore
from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import CATALOG, DATA_DIR
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.adapters.locks import FlockArchiveLock
from slarc.ports import Commit, SlarcError
from tests.contract.test_archive_store import (
    CHANNEL,
    KEY,
    R1,
    T1,
    TEAM,
    TS,
    file_line,
    message,
    tracked,
)
from tests.fakes import FakeArchiveLock

KILLED_CHANNEL = "C0KILLED001"

# Holds the archive's locks and leaves a large insert uncommitted until it is killed.
WRITER = """
import sys
import time
from pathlib import Path

from slarc.adapters.lake import connect_lake
from slarc.adapters.lake_store import MESSAGE_COLUMNS, insert_rows
from slarc.adapters.locks import FlockArchiveLock

root, cache, ext_dir, channel = sys.argv[1:]
root = Path(root)
lock = FlockArchiveLock(root, Path(cache))
lock.hold_shared()
with lock.write():
    con = connect_lake(root, Path(ext_dir))
    con.execute("BEGIN")
    rows = [
        {"team": "T1", "channel": channel, "ts": f"{i}.000100", "kind": "message",
         "fetched_at": "2026-10-20T00:00:00Z", "run": "r_killed", "hash": f"h{i}"}
        for i in range(5000)
    ]
    insert_rows(con, "message_version", MESSAGE_COLUMNS, rows)
    print("inserted", flush=True)
    time.sleep(60)
"""


def data_files(root: Path) -> set[Path]:
    return {path for path in (root / DATA_DIR).rglob("*") if path.is_file()}


def catalog_pages(root: Path) -> tuple[int, int]:
    """The catalog's free pages and all its pages."""
    con = sqlite3.connect(root / CATALOG)
    try:
        free = con.execute("PRAGMA freelist_count").fetchone()[0]
        pages = con.execute("PRAGMA page_count").fetchone()[0]
    finally:
        con.close()
    return free, pages


def messages(count: int, channel: str = CHANNEL) -> list:
    return [
        message(f"{1792410000 + i}.000100", f"m{i}", T1, R1, channel=channel) for i in range(count)
    ]


def test_maintain_compacts_then_deletes_under_the_exclusive_lock(
    tmp_path: Path, ext_dir: Path
) -> None:
    lock = FakeArchiveLock()
    store = DuckLakeArchiveStore(tmp_path, ext_dir, lock)
    del lock.events[:]
    assert store.maintain(FsBlobStore(tmp_path)) is True
    assert lock.events == [
        "write",
        "end write",
        "release_shared",
        "try_exclusive",
        "write",
        "end write",
        "release_exclusive",
        "hold_shared",
    ]
    store.close()


def test_maintain_physically_deletes_dropped_rows_and_unreferenced_blobs(
    tmp_path: Path, ext_dir: Path
) -> None:
    blobs = FsBlobStore(tmp_path)
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    kept, _ = blobs.write([b"kept"])
    blobs.write([b"unreferenced"])
    for env in messages(50):
        store.append(env)
    store.append(file_line("F1", T1, R1, kept))
    store.commit(Commit(items=[tracked(TS)]))
    written = data_files(tmp_path)
    store.drop_messages(TEAM, CHANNEL, {env.meta.ts for env in messages(50)})
    store.commit(Commit())

    assert store.maintain(blobs) is True
    assert written
    assert written.isdisjoint(data_files(tmp_path))
    assert sorted(blobs.ids()) == [kept]
    assert store.channels(TEAM) == []
    store.close()


def test_maintain_defers_deletion_while_another_command_holds_the_archive(
    tmp_path: Path, ext_dir: Path
) -> None:
    blobs = FsBlobStore(tmp_path)
    lock = FakeArchiveLock()
    store = DuckLakeArchiveStore(tmp_path, ext_dir, lock)
    unreferenced, _ = blobs.write([b"unreferenced"])
    for env in messages(50):
        store.append(env)
    store.commit(Commit(items=[tracked(TS)]))
    written = data_files(tmp_path)
    store.drop_messages(TEAM, CHANNEL, {env.meta.ts for env in messages(50)})
    store.commit(Commit())
    lock.exclusive_free = False
    del lock.events[:]

    assert store.maintain(blobs) is False
    assert lock.events == ["write", "end write", "release_shared", "try_exclusive", "hold_shared"]
    assert written
    assert written <= data_files(tmp_path)
    assert list(blobs.ids()) == [unreferenced]
    assert store.channels(TEAM) == []
    store.close()


def test_maintain_holds_the_read_lock_again_and_close_releases_it(
    tmp_path: Path, ext_dir: Path
) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    store = DuckLakeArchiveStore(root, ext_dir, FlockArchiveLock(root, cache))
    other = FlockArchiveLock(root, cache)
    assert store.maintain(FsBlobStore(root)) is True
    assert not other.try_exclusive()
    store.close()
    assert other.try_exclusive()
    other.release_exclusive()


def test_maintain_defers_deletion_while_this_process_has_an_index_open(
    tmp_path: Path, ext_dir: Path
) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    lock = FlockArchiveLock(root, cache)
    store = DuckLakeArchiveStore(root, ext_dir, lock)
    blobs = FsBlobStore(root)
    unreferenced, _ = blobs.write([b"unreferenced"])
    index = DuckDbQueryIndex(root, ext_dir, lock)

    assert store.maintain(blobs) is False
    assert list(blobs.ids()) == [unreferenced]
    index.close()
    assert store.maintain(blobs) is True
    assert list(blobs.ids()) == []
    store.close()
    other = FlockArchiveLock(root, cache)
    assert other.try_exclusive()
    other.release_exclusive()


def test_maintain_refuses_an_open_batch(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store.append(message(TS, "uncommitted", T1, R1))
    with pytest.raises(ValueError):
        store.maintain(FsBlobStore(tmp_path))
    store.close()


def leave_free_pages(root: Path) -> None:
    """Grows the catalog with a table and drops it, as expired snapshots leave it mostly free."""
    con = sqlite3.connect(root / CATALOG)
    try:
        con.execute("CREATE TABLE filler (data BLOB)")
        con.executemany("INSERT INTO filler VALUES (randomblob(4096))", [()] * 200)
        con.commit()
        con.execute("DROP TABLE filler")
        con.commit()
    finally:
        con.close()


def test_maintain_vacuums_a_catalog_left_mostly_free(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store.commit(Commit(items=[tracked(TS)]))
    leave_free_pages(tmp_path)
    free, before = catalog_pages(tmp_path)
    assert free * 4 > before
    assert store.maintain(FsBlobStore(tmp_path)) is True
    free, pages = catalog_pages(tmp_path)
    assert pages < before
    assert free * 4 <= pages
    store.close()


def test_maintenance_failure_is_a_slarc_error(tmp_path: Path, ext_dir: Path) -> None:
    lock = FakeArchiveLock()
    store = DuckLakeArchiveStore(tmp_path, ext_dir, lock)
    store._con.close()
    with pytest.raises(SlarcError, match="maintaining the archive failed"):
        store.maintain(FsBlobStore(tmp_path))
    assert lock.shared == 1


def test_killed_writer_leaves_last_commit_and_orphans_are_removed(
    tmp_path: Path, ext_dir: Path
) -> None:
    root, cache = tmp_path / "archive", tmp_path / "cache"
    store = DuckLakeArchiveStore(root, ext_dir, FlockArchiveLock(root, cache))
    for env in messages(50):
        store.append(env)
    store.commit(Commit(items=[tracked(TS)]))
    committed = store.stored_messages(TEAM, CHANNEL)
    store.close()
    before = data_files(root)

    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, root, cache, ext_dir, KILLED_CHANNEL],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    assert writer.stdout.readline() == "inserted\n"
    os.kill(writer.pid, signal.SIGKILL)
    writer.wait(timeout=30)
    leftover = data_files(root) - before
    assert leftover

    store = DuckLakeArchiveStore(root, ext_dir, FlockArchiveLock(root, cache))
    assert store.stored_messages(TEAM, CHANNEL) == committed
    assert store.channels("T1") == []
    assert store.maintain(FsBlobStore(root)) is True
    assert leftover.isdisjoint(data_files(root))
    assert store.stored_messages(TEAM, CHANNEL) == committed
    store.close()


def test_catalog_and_file_count_stay_bounded(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    blobs = FsBlobStore(tmp_path)
    item = tracked(TS)
    sizes = {}
    for number, env in enumerate(messages(30), start=1):
        store.append(env)
        committed = store.commit(Commit(items=[item]))
        item = msgspec.structs.replace(
            item, newest_ts=env.meta.ts, revision=committed.revisions[KEY]
        )
        store.maintain(blobs)
        sizes[number] = (tmp_path / CATALOG).stat().st_size
    assert len(data_files(tmp_path)) < 10
    assert sizes[30] == sizes[10]
    assert len(store.stored_messages(TEAM, CHANNEL)) == 30
    store.close()
