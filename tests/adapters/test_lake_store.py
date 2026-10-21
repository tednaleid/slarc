# ABOUTME: Tests for behavior only the lake store has: the open batch across connections, a run
# ABOUTME: row that a failed append_run leaves unwritten, tracked item round trips and lake errors.
from collections.abc import Callable
from pathlib import Path

import msgspec
import pytest

from slarc.adapters import lake_store
from slarc.adapters.lake import CATALOG, connect_lake
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.ports import ArchiveReadFailed, ArchiveWriteFailed, Commit
from tests.contract.test_archive_store import (
    CHANNEL,
    R1,
    R2,
    T1,
    T2,
    TEAM,
    TS,
    VERSION,
    file_line,
    message,
    run_record,
    tracked,
)
from tests.fakes import FakeArchiveLock


def test_store_holds_the_read_lock_and_writes_under_the_write_lock(
    tmp_path: Path, ext_dir: Path
) -> None:
    lock = FakeArchiveLock()
    store = DuckLakeArchiveStore(tmp_path, ext_dir, lock)
    assert lock.events == ["hold_shared", "write", "end write"]
    store.append(message(TS, "Hello", T1, R1))
    store.drop_messages(TEAM, CHANNEL, {TS})
    store.commit(Commit())
    store.append_run(run_record(R1, T1), VERSION)
    store.close()
    assert lock.events[3:] == ["write", "end write", "write", "end write", "release_shared"]


def test_lines_without_a_run_line_are_not_kept(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store.append(message(TS, "uncommitted", T1, R1))
    store.append(file_line("F1", T1, R1, None))
    store.close()

    reopened = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    assert reopened.stored_messages(TEAM, CHANNEL) == {}
    assert reopened.directory_state(TEAM, "file") == {}
    assert reopened.channels(TEAM) == []
    reopened.close()


def test_reads_see_the_open_batch(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    committed = message(TS, "Hello", T1, R1)
    store.append(committed)
    store.commit(Commit())
    edited = message(TS, "Hello!", T2, R2)
    downloaded = file_line("F1", T2, R2, "ab" * 32)
    store.append(edited)
    store.append(downloaded)
    other = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())

    assert store.stored_messages(TEAM, CHANNEL)[TS].hash == edited.meta.hash
    assert [env.meta.hash for env in store.message_lines(TEAM, CHANNEL)] == [
        committed.meta.hash,
        edited.meta.hash,
    ]
    assert store.channels(TEAM) == [CHANNEL]
    assert store.directory_state(TEAM, "file")["F1"].blob == "ab" * 32
    assert store.directory_objects(TEAM, "file") == {"F1": downloaded.object}
    assert other.stored_messages(TEAM, CHANNEL)[TS].hash == committed.meta.hash
    assert other.directory_state(TEAM, "file") == {}
    store.close()
    other.close()


def run_count(root: Path, ext_dir: Path) -> int:
    con = connect_lake(root, ext_dir)
    try:
        return con.execute("SELECT count(*) FROM run").fetchall()[0][0]
    finally:
        con.close()


def test_failed_append_run_writes_no_run_row(
    tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    run = run_record(R1, T1)

    def fail(*args: object) -> None:
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patched:
        patched.setattr(lake_store, "run_row", fail)
        with pytest.raises(RuntimeError):
            store.append_run(run, VERSION)
    assert run_count(tmp_path, ext_dir) == 0

    store.append_run(run, VERSION)
    store.close()
    assert run_count(tmp_path, ext_dir) == 1


def test_tracked_items_round_trip_every_field(tmp_path: Path, ext_dir: Path) -> None:
    thread = tracked("100.000000")
    thread.kind = "thread"
    thread.ts = "1.000001"
    thread.last_read = {"1.000001": "2.000002"}
    thread.retry_threads = ["3.000003"]
    thread.archived = True
    thread.backfill_complete = True
    conversation = tracked("200.000000")
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store.commit(Commit(items=[thread, conversation]))
    store.close()

    reopened = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    assert reopened.tracked_items() == {
        thread.key: msgspec.structs.replace(thread, revision=1),
        conversation.key: msgspec.structs.replace(conversation, revision=1),
    }
    reopened.close()


def assert_one_plain_sentence(message: str, path: Path) -> None:
    """Apart from the path it names, the message is one line with one full stop, at its end."""
    rest = message.replace(str(path), "")
    assert "\n" not in rest
    assert rest.endswith(".")
    assert not any(mark in rest[:-1] for mark in ".!?")


WRITES: dict[str, Callable[[DuckLakeArchiveStore], object]] = {
    "commit": lambda store: store.commit(Commit()),
    "append_run": lambda store: store.append_run(run_record(R1, T1), VERSION),
}
READS: dict[str, Callable[[DuckLakeArchiveStore], object]] = {
    "stored_messages": lambda store: store.stored_messages(TEAM, CHANNEL),
    "directory_state": lambda store: store.directory_state(TEAM, "file"),
    "directory_objects": lambda store: store.directory_objects(TEAM, "file"),
    "tracked_items": lambda store: store.tracked_items(),
    "removed_items": lambda store: store.removed_items(),
    "downloads": lambda store: store.downloads(),
    "channels": lambda store: store.channels(TEAM),
    "message_lines": lambda store: store.message_lines(TEAM, CHANNEL),
    "blob_references": lambda store: store.blob_references(),
    "drop_messages": lambda store: store.drop_messages(TEAM, CHANNEL, {TS}),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_a_lake_error_while_writing_is_an_archive_write_failure(
    tmp_path: Path, ext_dir: Path, write: Callable[[DuckLakeArchiveStore], object]
) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store._con.close()
    with pytest.raises(ArchiveWriteFailed) as caught:
        write(store)
    assert_one_plain_sentence(str(caught.value), tmp_path / CATALOG)


@pytest.mark.parametrize("read", READS.values(), ids=READS.keys())
def test_a_lake_error_while_reading_is_an_archive_read_failure(
    tmp_path: Path, ext_dir: Path, read: Callable[[DuckLakeArchiveStore], object]
) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store._con.close()
    with pytest.raises(ArchiveReadFailed) as caught:
        read(store)
    assert_one_plain_sentence(str(caught.value), tmp_path / CATALOG)
