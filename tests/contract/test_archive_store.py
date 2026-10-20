# ABOUTME: Contract tests every ArchiveStore must pass, run over the fake, JSONL and the lake.
# ABOUTME: Covers file layout, latest-wins reads, directory and run-line state, and dropping lines.
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import msgspec
import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.jsonl_store import JsonlArchiveStore
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.envelope import Envelope, Meta, decode_line, encode_line
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import (
    DownloadState,
    RunRecord,
    TrackedItem,
    run_envelope,
    run_from_envelope,
)
from slarc.ports import ArchiveStore, ArchiveWriteFailed, DirectoryState, SlarcError, StoredMessage
from tests.fakes import InMemoryArchiveStore

TEAM = "T0CB5LYPVFA"
CHANNEL = "C0CA5396G03"
TS = "1792410693.663869"  # 2026-10-19 UTC
OLD_TS = "1709300000.000100"  # 2024-03-01 UTC
LATER_TS = "1792410800.000200"  # 2026-10-19 UTC
OTHER_TEAM = "T0OTHER0001"
OTHER_CHANNEL = "C0OTHER0002"
VERSION = "0.1.0"

T1 = "2026-10-19T13:40:11Z"
T2 = "2026-10-19T14:00:00Z"
T3 = "2026-10-19T15:00:00Z"
R1 = "r_20261019T134011Z"
R2 = "r_20261019T140000Z"
R3 = "r_20261019T150000Z"
R_REOPEN = "r_reopen"


def message(
    ts: str,
    text: str,
    fetched: str,
    run: str,
    thread_ts: str | None = None,
    channel: str = CHANNEL,
    team: str = TEAM,
) -> Envelope:
    obj = {"type": "message", "user": "U1", "text": text, "ts": ts}
    if thread_ts is not None:
        obj["thread_ts"] = thread_ts
    meta = Meta(
        kind="message",
        team=team,
        channel=channel,
        ts=ts,
        source_team=TEAM,
        fetched_at=fetched,
        run=run,
        api="conversations.history",
        slarc=VERSION,
        hash=content_hash("message", obj),
        plain=text,
    )
    return Envelope(meta=meta, object=obj)


def deleted(ts: str, fetched: str, run: str) -> Envelope:
    meta = Meta(
        kind="deleted",
        team=TEAM,
        channel=CHANNEL,
        ts=ts,
        fetched_at=fetched,
        run=run,
        slarc=VERSION,
    )
    return Envelope(meta=meta)


def file_line(
    file_id: str,
    fetched: str,
    run: str,
    blob: str | None,
    team: str = TEAM,
    kind: Literal["file", "image"] = "file",
) -> Envelope:
    obj = {"id": file_id, "name": "notes.txt", "size": 5}
    meta = Meta(
        kind=kind,
        team=team,
        id=file_id,
        fetched_at=fetched,
        run=run,
        api="files.info",
        slarc=VERSION,
        hash=content_hash(kind, obj),
        blob=blob,
    )
    return Envelope(meta=meta, object=obj)


def run_record(run: str, started: str, **fields) -> RunRecord:
    return RunRecord(run=run, command="pull", started=started, finished=started, **fields)


def tracked(newest_ts: str, channel: str = CHANNEL) -> TrackedItem:
    return TrackedItem(team_id=TEAM, kind="conversation", channel=channel, newest_ts=newest_ts)


def download(
    target: str, status: Literal["pending", "failed", "gave_up", "removed"], attempts: int
) -> DownloadState:
    return DownloadState(
        team_id=TEAM, target_kind="file", target=target, status=status, attempts=attempts
    )


@pytest.fixture(params=["memory", "jsonl", "lake"])
def store_and_reopen(
    request, tmp_path: Path, ext_dir: Path
) -> Iterator[tuple[ArchiveStore, Callable[[], ArchiveStore]]]:
    """Yields a store and a function returning a store over the same archive.

    For the JSONL store the reopened instance reads only what reached the files. The lake
    keeps only committed lines, so reopening it first commits each open store's batch with
    a run that tracks nothing.
    """
    if request.param == "memory":
        memory = InMemoryArchiveStore()
        yield memory, lambda: memory
        return
    opened: list[JsonlArchiveStore | DuckLakeArchiveStore]
    if request.param == "jsonl":
        opened = [JsonlArchiveStore(tmp_path)]
    else:
        opened = [DuckLakeArchiveStore(tmp_path, ext_dir)]

    def reopen() -> ArchiveStore:
        current = opened[-1]
        if isinstance(current, JsonlArchiveStore):
            current.flush()
            opened.append(JsonlArchiveStore(tmp_path))
        else:
            for store in opened:
                store.append_run(run_record(R_REOPEN, T1), VERSION)
            opened.append(DuckLakeArchiveStore(tmp_path, ext_dir))
        return opened[-1]

    yield opened[0], reopen
    for store in opened:
        store.close()


@pytest.fixture
def jsonl_store(tmp_path: Path) -> Iterator[JsonlArchiveStore]:
    store = JsonlArchiveStore(tmp_path)
    yield store
    store.close()


def test_message_lands_in_month_file_of_its_ts(jsonl_store: JsonlArchiveStore, tmp_path: Path):
    jsonl_store.append(message(OLD_TS, "old", T1, R1))
    jsonl_store.append(message(TS, "new", T1, R1))
    jsonl_store.append(deleted(OLD_TS, T2, R2))
    jsonl_store.append(file_line("F1", T1, R1, None))
    jsonl_store.flush()

    old_month = tmp_path / "raw" / TEAM / CHANNEL / "2024-03.jsonl"
    new_month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    old_kinds = [decode_line(line).meta.kind for line in old_month.read_bytes().splitlines()]
    assert old_kinds == ["message", "deleted"]
    assert len(new_month.read_bytes().splitlines()) == 1
    assert (tmp_path / "directory" / TEAM / "files.jsonl").exists()


def test_stored_messages_reflects_latest_line(store_and_reopen):
    store, reopen = store_and_reopen
    first = message(TS, "Hello", T1, R1)
    edited = message(TS, "Hello!", T2, R2)
    store.append(first)
    store.append(edited)

    expected = StoredMessage(hash=edited.meta.hash, thread_ts=None, fetched_at=T2)
    assert store.stored_messages(TEAM, CHANNEL) == {TS: expected}

    again = message(TS, "Hello!!", T3, R3, thread_ts=TS)
    store.append(again)
    latest = StoredMessage(hash=again.meta.hash, thread_ts=TS, fetched_at=T3)
    assert store.stored_messages(TEAM, CHANNEL) == {TS: latest}
    assert reopen().stored_messages(TEAM, CHANNEL) == {TS: latest}


def test_deleted_line_sets_hash_none(store_and_reopen):
    store, reopen = store_and_reopen
    reply_ts = "1792410700.000100"
    store.append(message(reply_ts, "a reply", T1, R1, thread_ts=TS))
    store.append(deleted(reply_ts, T2, R2))

    expected = {reply_ts: StoredMessage(hash=None, thread_ts=TS, fetched_at=T2)}
    assert store.stored_messages(TEAM, CHANNEL) == expected
    assert reopen().stored_messages(TEAM, CHANNEL) == expected


def test_directory_state_includes_blob(store_and_reopen):
    store, reopen = store_and_reopen
    referenced = file_line("F1", T1, R1, None)
    downloaded = file_line("F1", T2, R2, "ab" * 32)
    store.append(referenced)
    store.append(downloaded)
    assert downloaded.meta.hash is not None

    for current in (store, reopen()):
        state = current.directory_state(TEAM, "file")
        assert state == {"F1": DirectoryState(hash=downloaded.meta.hash, blob="ab" * 32)}
        assert current.directory_objects(TEAM, "file") == {"F1": downloaded.object}
        assert current.directory_state(TEAM, "user") == {}


def test_duplicate_and_out_of_order_lines_tolerated(store_and_reopen):
    store, reopen = store_and_reopen
    newer = message(TS, "newer", T2, R2)
    older = message(TS, "older", T1, R1)
    store.append(newer)
    store.append(older)
    store.append(newer)
    store.append(file_line("F1", T2, R2, "cd" * 32))
    store.append(file_line("F1", T1, R1, None))

    expected = {TS: StoredMessage(hash=newer.meta.hash, thread_ts=None, fetched_at=T2)}
    for current in (store, reopen()):
        assert current.stored_messages(TEAM, CHANNEL) == expected
        assert current.directory_state(TEAM, "file")["F1"].blob == "cd" * 32


def test_same_second_blob_line_wins_in_store_and_index(store_and_reopen, tmp_path: Path):
    """A blob line appended in the same second and run as the line without it is current,
    in the store's fold and in the DuckDB file view."""
    store, reopen = store_and_reopen
    without_blob = file_line("F1", T1, R1, None)
    store.append(without_blob)
    store.append(file_line("F1", T1, R1, "cd" * 32))

    expected = DirectoryState(hash=without_blob.meta.hash or "", blob="cd" * 32)
    for current in (store, reopen()):
        assert current.directory_state(TEAM, "file")["F1"] == expected
    if isinstance(store, JsonlArchiveStore):
        with DuckDbQueryIndex(tmp_path) as index:
            assert index.sql("SELECT blob FROM file WHERE id = 'F1'")[1] == [("cd" * 32,)]


def test_tracked_items_latest_entry_across_runs(store_and_reopen):
    store, reopen = store_and_reopen
    if isinstance(store, DuckLakeArchiveStore):
        pytest.skip("lake commits are serialized, so the last committed run wins")
    other = "C0OTHER0001"
    store.append_run(run_record(R2, T2, tracked=[tracked("200.000000")]), VERSION)
    store.append_run(
        run_record(R1, T1, tracked=[tracked("100.000000"), tracked("50.000000", other)]), VERSION
    )

    for current in (store, reopen()):
        items = current.tracked_items()
        assert items[(TEAM, "conversation", CHANNEL, "")].newest_ts == "200.000000"
        assert items[(TEAM, "conversation", other, "")].newest_ts == "50.000000"
        assert len(items) == 2


def test_removed_items_leave_tracked_items(store_and_reopen):
    store, reopen = store_and_reopen
    keep = tracked("100.000000")
    gone = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts="1.000001")
    store.append_run(run_record(R1, T1, tracked=[keep, gone]), VERSION)
    store.append_run(
        run_record(R2, T2, tracked=[msgspec.structs.replace(gone, status="removed")]), VERSION
    )

    for current in (store, reopen()):
        assert list(current.tracked_items()) == [keep.key]
        assert list(current.removed_items()) == [gone.key]


def test_retracking_a_removed_item_tracks_it_again(store_and_reopen):
    store, reopen = store_and_reopen
    item = tracked("100.000000")
    store.append_run(
        run_record(R1, T1, tracked=[msgspec.structs.replace(item, status="removed")]), VERSION
    )
    store.append_run(run_record(R2, T2, tracked=[item]), VERSION)

    for current in (store, reopen()):
        assert list(current.tracked_items()) == [item.key]
        assert current.removed_items() == {}


def test_archived_and_new_statuses_round_trip_through_a_run_line():
    run = run_record(
        R1,
        T1,
        tracked=[msgspec.structs.replace(tracked("1.0"), archived=True, status="removed")],
        downloads=[download("F1", "gave_up", 3), download("F2", "removed", 0)],
    )
    decoded = run_from_envelope(decode_line(encode_line(run_envelope(run, VERSION))))
    assert decoded == run


def test_run_line_without_archived_decodes_as_not_archived():
    env = run_envelope(run_record(R1, T1, tracked=[tracked("1.0")]), VERSION)
    assert env.object is not None
    del env.object["tracked"][0]["archived"]
    assert run_from_envelope(env).tracked[0].archived is False


def test_downloads_latest_entry_across_runs(store_and_reopen):
    store, reopen = store_and_reopen
    store.append_run(run_record(R1, T1, downloads=[download("F1", "pending", 0)]), VERSION)
    store.append_run(
        run_record(R2, T2, downloads=[download("F1", "failed", 1), download("F2", "pending", 0)]),
        VERSION,
    )

    for current in (store, reopen()):
        states = current.downloads()
        assert states[(TEAM, "F1")] == download("F1", "failed", 1)
        assert states[(TEAM, "F2")] == download("F2", "pending", 0)
        assert len(states) == 2


def test_downloads_keep_a_downloaded_targets_earlier_state(store_and_reopen):
    """A downloaded target drops out of its run's list; its directory blob records the download."""
    store, reopen = store_and_reopen
    store.append_run(run_record(R1, T1, downloads=[download("F1", "pending", 0)]), VERSION)
    store.append(file_line("F1", T2, R2, "ab" * 32))
    store.append_run(run_record(R2, T2), VERSION)

    for current in (store, reopen()):
        assert current.downloads() == {(TEAM, "F1"): download("F1", "pending", 0)}


def test_run_lines_appended_after_a_read_fold_in_version_order(store_and_reopen):
    store, reopen = store_and_reopen
    if isinstance(store, DuckLakeArchiveStore):
        pytest.skip("lake commits are serialized, so the last committed run wins")
    store.append_run(run_record(R1, T1, tracked=[tracked("100.000000")]), VERSION)
    store.downloads()
    store.append_run(
        run_record(
            R3, T3, tracked=[tracked("300.000000")], downloads=[download("F1", "failed", 1)]
        ),
        VERSION,
    )
    store.append_run(
        run_record(
            R2, T2, tracked=[tracked("200.000000")], downloads=[download("F1", "pending", 0)]
        ),
        VERSION,
    )

    for current in (store, reopen()):
        assert (
            current.tracked_items()[(TEAM, "conversation", CHANNEL, "")].newest_ts == "300.000000"
        )
        assert current.downloads()[(TEAM, "F1")] == download("F1", "failed", 1)


def test_jsonl_store_reads_runs_jsonl_once(
    jsonl_store: JsonlArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs = tmp_path / "runs.jsonl"
    jsonl_store.append_run(run_record(R1, T1, tracked=[tracked("100.000000")]), VERSION)
    reads: list[Path] = []
    real_read = JsonlArchiveStore._read

    def counting_read(self, paths):
        paths = list(paths)
        reads.extend(paths)
        return real_read(self, paths)

    monkeypatch.setattr(JsonlArchiveStore, "_read", counting_read)
    jsonl_store.tracked_items()
    jsonl_store.removed_items()
    jsonl_store.downloads()
    jsonl_store.append_run(run_record(R2, T2, tracked=[tracked("200.000000")]), VERSION)
    items = jsonl_store.tracked_items()

    assert reads.count(runs) == 1
    assert items[(TEAM, "conversation", CHANNEL, "")].newest_ts == "200.000000"


def test_truncated_trailing_line_skipped_and_next_append_starts_new_line(tmp_path: Path):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    month.parent.mkdir(parents=True)
    good = message(TS, "kept", T1, R1)
    month.write_bytes(encode_line(good) + encode_line(message(TS, "cut", T2, R2))[:40])

    store = JsonlArchiveStore(tmp_path)
    assert store.stored_messages(TEAM, CHANNEL)[TS].hash == good.meta.hash

    later = message(TS, "later", T3, R3)
    store.append(later)
    store.close()

    reopened = JsonlArchiveStore(tmp_path)
    assert reopened.stored_messages(TEAM, CHANNEL)[TS].hash == later.meta.hash
    reopened.close()


def test_line_of_unknown_kind_skipped(tmp_path: Path):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    month.parent.mkdir(parents=True)
    unknown = b'{"meta":{"schema":1,"kind":"huddle","team":"T","ts":"1.0"},"object":{}}\n'
    good = message(TS, "kept", T1, R1)
    month.write_bytes(unknown + encode_line(good))

    store = JsonlArchiveStore(tmp_path)
    assert store.stored_messages(TEAM, CHANNEL) == {
        TS: StoredMessage(hash=good.meta.hash, thread_ts=None, fetched_at=T1)
    }
    store.close()


def test_jsonl_store_in_a_read_only_archive_raises_slarc_error(tmp_path: Path):
    tmp_path.chmod(0o555)
    store = JsonlArchiveStore(tmp_path)
    try:
        with pytest.raises(SlarcError, match=f"Could not write to {tmp_path / 'runs.jsonl'}"):
            store.append_run(run_record(R1, T1), VERSION)
    finally:
        store.close()
        tmp_path.chmod(0o755)


def stamps(store: ArchiveStore, team: str = TEAM, channel: str = CHANNEL) -> list[str | None]:
    return [env.meta.ts for env in store.message_lines(team, channel)]


def test_drop_messages_removes_every_version_and_only_those(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(OLD_TS, "old", T1, R1))
    store.append(message(TS, "Hello", T1, R1))
    store.append(message(TS, "Hello!", T2, R2))
    store.append(message(LATER_TS, "kept", T1, R1))
    store.append(deleted(TS, T3, R3))
    store.append(message(TS, "elsewhere", T1, R1, channel=OTHER_CHANNEL))

    assert store.drop_messages(TEAM, CHANNEL, {TS}) == 3

    for current in (store, reopen()):
        assert stamps(current) == [OLD_TS, LATER_TS]
        assert stamps(current, channel=OTHER_CHANNEL) == [TS]


def test_drop_messages_deletes_empty_month_files_and_channel_dir(
    jsonl_store: JsonlArchiveStore, tmp_path: Path
):
    channel_dir = tmp_path / "raw" / TEAM / CHANNEL
    jsonl_store.append(message(OLD_TS, "old", T1, R1))
    jsonl_store.append(message(TS, "new", T1, R1))

    assert jsonl_store.drop_messages(TEAM, CHANNEL, {OLD_TS}) == 1
    assert sorted(p.name for p in channel_dir.iterdir()) == ["2026-10.jsonl"]

    assert jsonl_store.drop_messages(TEAM, CHANNEL, {TS}) == 1
    assert not channel_dir.exists()
    assert jsonl_store.channels(TEAM) == []


def test_drop_messages_refreshes_stored_messages(store_and_reopen):
    store, reopen = store_and_reopen
    kept = message(LATER_TS, "kept", T1, R1)
    store.append(message(TS, "gone", T1, R1))
    store.append(kept)
    assert set(store.stored_messages(TEAM, CHANNEL)) == {TS, LATER_TS}

    store.drop_messages(TEAM, CHANNEL, {TS})

    expected = {LATER_TS: StoredMessage(hash=kept.meta.hash, thread_ts=None, fetched_at=T1)}
    for current in (store, reopen()):
        assert current.stored_messages(TEAM, CHANNEL) == expected


def test_append_after_drop_messages_still_lands(store_and_reopen):
    store, reopen = store_and_reopen
    third = "1792410900.000300"
    fourth = "1792411000.000400"
    store.append(message(TS, "first", T1, R1))
    store.append(message(LATER_TS, "second", T1, R1))
    store.stored_messages(TEAM, CHANNEL)

    store.drop_messages(TEAM, CHANNEL, {TS})
    store.append(message(third, "third", T2, R2))
    assert set(reopen().stored_messages(TEAM, CHANNEL)) == {LATER_TS, third}

    store.drop_messages(TEAM, CHANNEL, {LATER_TS, third})
    store.append(message(fourth, "fourth", T3, R3))
    assert set(store.stored_messages(TEAM, CHANNEL)) == {fourth}
    assert set(reopen().stored_messages(TEAM, CHANNEL)) == {fourth}


def test_drop_directory_lines_removes_every_version(store_and_reopen):
    store, reopen = store_and_reopen
    kept = file_line("F2", T1, R1, None)
    store.append(file_line("F1", T1, R1, None))
    store.append(kept)
    store.append(file_line("F1", T2, R2, "ab" * 32))
    store.append(file_line("F1", T1, R1, "cd" * 32, kind="image"))

    assert store.drop_directory_lines(TEAM, "file", {"F1"}) == 2

    for current in (store, reopen()):
        assert current.directory_state(TEAM, "file") == {
            "F2": DirectoryState(hash=kept.meta.hash or "", blob=None)
        }
        assert list(current.directory_state(TEAM, "image")) == ["F1"]


def test_drop_workspace_removes_raw_and_directory(store_and_reopen, tmp_path: Path):
    store, reopen = store_and_reopen
    store.append(message(TS, "gone", T1, R1))
    store.append(file_line("F1", T1, R1, "ab" * 32))
    store.append(message(TS, "kept", T1, R1, team=OTHER_TEAM))
    store.append(file_line("F2", T1, R1, None, team=OTHER_TEAM))
    store.append_run(run_record(R1, T1, tracked=[tracked("100.000000")]), VERSION)
    store.stored_messages(TEAM, CHANNEL)

    store.drop_workspace(TEAM)

    for current in (store, reopen()):
        assert current.stored_messages(TEAM, CHANNEL) == {}
        assert current.channels(TEAM) == []
        assert current.directory_state(TEAM, "file") == {}
        assert set(current.stored_messages(OTHER_TEAM, CHANNEL)) == {TS}
        assert list(current.directory_state(OTHER_TEAM, "file")) == ["F2"]
        assert len(current.tracked_items()) == 1
    if isinstance(store, JsonlArchiveStore):
        assert not (tmp_path / "raw" / TEAM).exists()
        assert not (tmp_path / "directory" / TEAM).exists()

    store.append(message(LATER_TS, "again", T2, R2))
    store.flush()
    assert set(reopen().stored_messages(TEAM, CHANNEL)) == {LATER_TS}


def test_blob_references_lists_every_directory_blob(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(file_line("F1", T1, R1, None))
    store.append(file_line("F1", T2, R2, "ab" * 32))
    store.append(file_line("I1", T1, R1, "cd" * 32, team=OTHER_TEAM, kind="image"))
    store.append(message(TS, "no blob", T1, R1))

    expected = [
        (TEAM, "file", "F1", "ab" * 32),
        (OTHER_TEAM, "image", "I1", "cd" * 32),
    ]
    for current in (store, reopen()):
        assert sorted(current.blob_references()) == expected


def test_channels_lists_channels_with_lines(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "here", T1, R1, channel=OTHER_CHANNEL))
    store.append(message(TS, "there", T1, R1))
    store.append(file_line("F1", T1, R1, None))

    for current in (store, reopen()):
        assert current.channels(TEAM) == [CHANNEL, OTHER_CHANNEL]
        assert current.channels(OTHER_TEAM) == []


def test_rewrite_keeps_lines_that_do_not_decode(jsonl_store: JsonlArchiveStore, tmp_path: Path):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    month.parent.mkdir(parents=True)
    unknown = b'{"meta":{"schema":1,"kind":"huddle","team":"T","ts":"1.0"},"object":{}}\n'
    kept = encode_line(message(LATER_TS, "kept", T1, R1))
    month.write_bytes(unknown + encode_line(message(TS, "gone", T1, R1)) + kept)

    assert jsonl_store.drop_messages(TEAM, CHANNEL, {TS}) == 1
    assert month.read_bytes() == unknown + kept


def test_failed_rewrite_leaves_original_intact(
    jsonl_store: JsonlArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    jsonl_store.append(message(TS, "first", T1, R1))
    jsonl_store.append(message(LATER_TS, "second", T1, R1))
    jsonl_store.flush()
    original = month.read_bytes()

    def fail(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("slarc.adapters.jsonl_store.os.fsync", fail)
    with pytest.raises(ArchiveWriteFailed, match="No space left on device"):
        jsonl_store.drop_messages(TEAM, CHANNEL, {TS})

    assert month.read_bytes() == original
    assert list(month.parent.iterdir()) == [month]


def test_interrupted_rewrite_leaves_original_intact(
    jsonl_store: JsonlArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    jsonl_store.append(message(TS, "first", T1, R1))
    jsonl_store.append(message(LATER_TS, "second", T1, R1))
    jsonl_store.flush()
    original = month.read_bytes()

    def interrupt(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("slarc.adapters.jsonl_store.os.fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        jsonl_store.drop_messages(TEAM, CHANNEL, {TS})

    assert month.read_bytes() == original
    assert list(month.parent.iterdir()) == [month]


def test_rewritten_file_keeps_its_mode(jsonl_store: JsonlArchiveStore, tmp_path: Path):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    jsonl_store.append(message(TS, "first", T1, R1))
    jsonl_store.append(message(LATER_TS, "second", T1, R1))
    jsonl_store.flush()
    month.chmod(0o644)

    jsonl_store.drop_messages(TEAM, CHANNEL, {TS})

    assert month.stat().st_mode & 0o777 == 0o644


def test_append_run_syncs_data_lines_before_the_run_line_and_the_run_line_after(
    jsonl_store: JsonlArchiveStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    month = tmp_path / "raw" / TEAM / CHANNEL / "2026-10.jsonl"
    runs = tmp_path / "runs.jsonl"
    jsonl_store.append(message(TS, "first", T1, R1))
    synced: list[tuple[int, bool]] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced.append((os.fstat(fd).st_ino, runs.exists() and runs.stat().st_size > 0))
        real_fsync(fd)

    monkeypatch.setattr("slarc.adapters.jsonl_store.os.fsync", record)
    jsonl_store.append_run(run_record(R1, T1), VERSION)

    assert (month.stat().st_ino, False) in synced
    assert synced[-1] == (runs.stat().st_ino, True)
