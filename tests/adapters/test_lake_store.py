# ABOUTME: Tests for behavior only the lake store has: the open batch, which append_run commits
# ABOUTME: in one transaction with the run's tracked items and download states.
from pathlib import Path

import pytest

from slarc.adapters.lake_store import DuckLakeArchiveStore
from tests.contract.test_archive_store import (
    CHANNEL,
    R1,
    R2,
    T1,
    T2,
    TEAM,
    TS,
    VERSION,
    download,
    file_line,
    message,
    run_record,
    tracked,
)


def test_lines_without_a_run_line_are_not_kept(tmp_path: Path, ext_dir: Path) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    store.append(message(TS, "uncommitted", T1, R1))
    store.append(file_line("F1", T1, R1, None))
    store.flush()
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


def test_append_run_commits_batch_items_and_downloads_together(
    tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    line = message(TS, "Hello", T1, R1)
    run = run_record(
        R1, T1, tracked=[tracked("100.000000")], downloads=[download("F1", "pending", 0)]
    )
    store.append(line)

    def fail(*args: object) -> None:
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patched:
        patched.setattr(DuckLakeArchiveStore, "_upsert_downloads", fail)
        with pytest.raises(RuntimeError):
            store.append_run(run, VERSION)
    other = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert other.stored_messages(TEAM, CHANNEL) == {}
    assert other.tracked_items() == {}
    assert other.downloads() == {}

    store.append_run(run, VERSION)
    store.close()
    for current in (other, DuckLakeArchiveStore(tmp_path, ext_dir)):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}
        assert list(current.tracked_items()) == [tracked("100.000000").key]
        assert current.downloads() == {(TEAM, "F1"): download("F1", "pending", 0)}
        current.close()


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
    store.append_run(run_record(R1, T1, tracked=[thread, conversation]), VERSION)
    store.close()

    reopened = DuckLakeArchiveStore(tmp_path, ext_dir)
    assert reopened.tracked_items() == {thread.key: thread, conversation.key: conversation}
    reopened.close()
