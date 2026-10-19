# ABOUTME: Contract tests every ArchiveStore implementation must pass, run over the fake and JSONL.
# ABOUTME: Covers file layout, latest-wins reads, deleted lines, directory state and run-line state.
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import pytest

from slarc.adapters.jsonl_store import JsonlArchiveStore
from slarc.domain.envelope import Envelope, Meta, decode_line, encode_line
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem
from slarc.ports import ArchiveStore, DirectoryState, StoredMessage
from tests.fakes import InMemoryArchiveStore

TEAM = "T0CB5LYPVFA"
CHANNEL = "C0CA5396G03"
TS = "1792410693.663869"  # 2026-10-19 UTC
OLD_TS = "1709300000.000100"  # 2024-03-01 UTC
VERSION = "0.1.0"

T1 = "2026-10-19T13:40:11Z"
T2 = "2026-10-19T14:00:00Z"
T3 = "2026-10-19T15:00:00Z"
R1 = "r_20261019T134011Z"
R2 = "r_20261019T140000Z"
R3 = "r_20261019T150000Z"


def message(ts: str, text: str, fetched: str, run: str, thread_ts: str | None = None) -> Envelope:
    obj = {"type": "message", "user": "U1", "text": text, "ts": ts}
    if thread_ts is not None:
        obj["thread_ts"] = thread_ts
    meta = Meta(
        kind="message",
        team=TEAM,
        channel=CHANNEL,
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


def file_line(file_id: str, fetched: str, run: str, blob: str | None) -> Envelope:
    obj = {"id": file_id, "name": "notes.txt", "size": 5}
    meta = Meta(
        kind="file",
        team=TEAM,
        id=file_id,
        fetched_at=fetched,
        run=run,
        api="files.info",
        slarc=VERSION,
        hash=content_hash("file", obj),
        blob=blob,
    )
    return Envelope(meta=meta, object=obj)


def run_record(run: str, started: str, **fields) -> RunRecord:
    return RunRecord(run=run, command="pull", started=started, finished=started, **fields)


def tracked(newest_ts: str, channel: str = CHANNEL) -> TrackedItem:
    return TrackedItem(team_id=TEAM, kind="conversation", channel=channel, newest_ts=newest_ts)


def download(target: str, status: Literal["pending", "failed"], attempts: int) -> DownloadState:
    return DownloadState(
        team_id=TEAM, target_kind="file", target=target, status=status, attempts=attempts
    )


@pytest.fixture(params=["memory", "jsonl"])
def store_and_reopen(
    request, tmp_path: Path
) -> Iterator[tuple[ArchiveStore, Callable[[], ArchiveStore]]]:
    """Yields a store and a function returning a store over the same archive.

    For the JSONL store the reopened instance reads only what reached the files.
    """
    if request.param == "memory":
        memory = InMemoryArchiveStore()
        yield memory, lambda: memory
        return
    opened: list[JsonlArchiveStore] = [JsonlArchiveStore(tmp_path)]

    def reopen() -> ArchiveStore:
        opened[-1].flush()
        opened.append(JsonlArchiveStore(tmp_path))
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


def test_tracked_items_latest_entry_across_runs(store_and_reopen):
    store, reopen = store_and_reopen
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
