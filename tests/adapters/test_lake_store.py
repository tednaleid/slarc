# ABOUTME: Tests for behavior only the lake store has: the open batch, which append_run commits
# ABOUTME: in one transaction with the run row, and tracked item round trips.
from pathlib import Path

import msgspec
import pytest

from slarc.adapters import lake_store
from slarc.adapters.lake import connect_lake
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.ports import Commit
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


def test_lines_without_a_run_line_are_not_kept(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    store.append(message(TS, "uncommitted", T1, R1))
    store.append(file_line("F1", T1, R1, None))
    store.close()

    reopened = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert reopened.stored_messages(TEAM, CHANNEL) == {}
    assert reopened.directory_state(TEAM, "file") == {}
    assert reopened.channels(TEAM) == []
    reopened.close()


def test_reads_see_the_open_batch(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    committed = message(TS, "Hello", T1, R1)
    store.append(committed)
    store.append_run(run_record(R1, T1), VERSION)
    edited = message(TS, "Hello!", T2, R2)
    downloaded = file_line("F1", T2, R2, "ab" * 32)
    store.append(edited)
    store.append(downloaded)
    other = DuckLakeArchiveStore(tmp_path, ext_dir)

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


def test_append_run_commits_batch_and_run_together(
    tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    line = message(TS, "Hello", T1, R1)
    run = run_record(R1, T1)
    store.append(line)

    def fail(*args: object) -> None:
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patched:
        patched.setattr(lake_store, "run_row", fail)
        with pytest.raises(RuntimeError):
            store.append_run(run, VERSION)
    other = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert other.stored_messages(TEAM, CHANNEL) == {}
    assert run_count(tmp_path, ext_dir) == 0

    store.append_run(run, VERSION)
    store.close()
    for current in (other, DuckLakeArchiveStore(tmp_path, ext_dir)):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}
        current.close()
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
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    store.commit(Commit(items=[thread, conversation]))
    store.close()

    reopened = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert reopened.tracked_items() == {
        thread.key: msgspec.structs.replace(thread, revision=1),
        conversation.key: msgspec.structs.replace(conversation, revision=1),
    }
    reopened.close()
