# ABOUTME: Contract tests every ArchiveStore must pass, run over the fake and the lake.
# ABOUTME: Covers latest-wins reads, commits with revision checks, and staged drops.
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import msgspec
import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import connect_lake
from slarc.adapters.lake_store import (
    RUN_COLUMNS,
    VERSION_ORDER,
    DuckLakeArchiveStore,
    run_line,
)
from slarc.domain.envelope import Envelope, Meta, decode_line, encode_line
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import (
    DownloadState,
    RunRecord,
    TrackedItem,
    run_envelope,
    run_from_envelope,
)
from slarc.ports import ArchiveStore, Commit, CommitConflict, DirectoryState, StoredMessage
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
KEY = (TEAM, "conversation", CHANNEL, "")


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
    target: str, status: Literal["pending", "failed", "gave_up"], attempts: int
) -> DownloadState:
    return DownloadState(
        team_id=TEAM, target_kind="file", target=target, status=status, attempts=attempts
    )


@pytest.fixture(params=["memory", "lake"])
def store_and_reopen(
    request, tmp_path: Path, ext_dir: Path
) -> Iterator[tuple[ArchiveStore, Callable[[], ArchiveStore]]]:
    """Yields a store and a function returning a store over the same archive.

    Only committed lines are kept, so reopening first commits each open store's batch, then
    records a run that tracks nothing.
    """
    if request.param == "memory":
        memory = InMemoryArchiveStore()

        def reopen_memory() -> ArchiveStore:
            memory.commit(Commit())
            memory.append_run(run_record(R_REOPEN, T1), VERSION)
            return memory

        yield memory, reopen_memory
        return
    opened = [DuckLakeArchiveStore(tmp_path, ext_dir)]

    def reopen() -> ArchiveStore:
        for store in opened:
            store.commit(Commit())
            store.append_run(run_record(R_REOPEN, T1), VERSION)
        opened.append(DuckLakeArchiveStore(tmp_path, ext_dir))
        return opened[-1]

    yield opened[0], reopen
    for store in opened:
        store.close()


def track(store: ArchiveStore, *teams: str) -> None:
    """Commits a tracked conversation in each team, so the teams' directory lines commit."""
    items = [TrackedItem(team_id=team, kind="conversation", channel=CHANNEL) for team in teams]
    store.commit(Commit(items=items))


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
    track(store, TEAM)
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
    track(store, TEAM)
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


def test_same_second_blob_line_wins_in_store_and_index(
    store_and_reopen, tmp_path: Path, ext_dir: Path
):
    """A blob line appended in the same second and run as the line without it is current,
    in the store's fold and in the DuckDB file view."""
    store, reopen = store_and_reopen
    track(store, TEAM)
    without_blob = file_line("F1", T1, R1, None)
    store.append(without_blob)
    store.append(file_line("F1", T1, R1, "cd" * 32))

    expected = DirectoryState(hash=without_blob.meta.hash or "", blob="cd" * 32)
    for current in (store, reopen()):
        assert current.directory_state(TEAM, "file")["F1"] == expected
    if isinstance(store, DuckLakeArchiveStore):
        with DuckDbQueryIndex(tmp_path, ext_dir) as index:
            assert index.sql("SELECT blob FROM file WHERE id = 'F1'")[1] == [("cd" * 32,)]


def test_tracked_items_last_commit_wins(store_and_reopen):
    store, reopen = store_and_reopen
    other = "C0OTHER0001"
    store.commit(Commit(items=[tracked("200.000000")]))
    store.commit(
        Commit(
            items=[
                msgspec.structs.replace(tracked("100.000000"), revision=1),
                tracked("50.000000", other),
            ]
        )
    )

    for current in (store, reopen()):
        items = current.tracked_items()
        assert items[(TEAM, "conversation", CHANNEL, "")].newest_ts == "100.000000"
        assert items[(TEAM, "conversation", other, "")].newest_ts == "50.000000"
        assert len(items) == 2


def test_append_run_leaves_tracked_items_to_commits(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")]))
    stale = msgspec.structs.replace(tracked("200.000000"), revision=7)
    newcomer = tracked("50.000000", OTHER_CHANNEL)
    store.append_run(run_record(R1, T1, tracked=[stale, newcomer]), VERSION)

    expected = msgspec.structs.replace(tracked("100.000000"), revision=1)
    for current in (store, reopen()):
        assert current.tracked_items() == {KEY: expected}


def recorded_runs(store: ArchiveStore, root: Path, ext_dir: Path) -> list[RunRecord]:
    """Every run the store recorded, read past the port, which has no run reads."""
    if isinstance(store, InMemoryArchiveStore):
        lines = [env for env in store.lines if env.meta.kind == "run"]
    else:
        names = ", ".join(RUN_COLUMNS)
        con = connect_lake(root, ext_dir)
        try:
            rows = con.execute(f"SELECT {names} FROM run ORDER BY {VERSION_ORDER}").fetchall()
        finally:
            con.close()
        lines = [run_line(dict(zip(RUN_COLUMNS, row, strict=True))) for row in rows]
    return [run_from_envelope(env) for env in lines]


def test_append_run_writes_only_the_run_row(store_and_reopen, tmp_path: Path, ext_dir: Path):
    store, reopen = store_and_reopen
    store.append(message(TS, "kept", T1, R1))
    store.commit(Commit(items=[tracked("100.000000")], downloads=[download("F1", "pending", 0)]))
    run = run_record(
        R2,
        T2,
        teams=[TEAM],
        tracked=[tracked("200.000000", OTHER_CHANNEL)],
        downloads=[download("F2", "pending", 0)],
    )

    store.append(message(LATER_TS, "open", T2, R2))
    with pytest.raises(ValueError):
        store.append_run(run, VERSION)
    store.discard()
    store.drop_messages(TEAM, CHANNEL, {TS})
    with pytest.raises(ValueError):
        store.append_run(run, VERSION)
    store.discard()
    store.drop_downloads(TEAM, {"F1"})
    with pytest.raises(ValueError):
        store.append_run(run, VERSION)
    store.discard()
    store.append_run(run, VERSION)

    assert recorded_runs(store, tmp_path, ext_dir) == [
        msgspec.structs.replace(run, tracked=[], downloads=[])
    ]
    expected = msgspec.structs.replace(tracked("100.000000"), revision=1)
    for current in (store, reopen()):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}
        assert current.tracked_items() == {KEY: expected}
        assert list(current.downloads()) == [(TEAM, "F1")]


def test_removed_items_leave_tracked_items(store_and_reopen):
    store, reopen = store_and_reopen
    keep = tracked("100.000000")
    gone = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts="1.000001")
    store.commit(Commit(items=[keep, gone]))
    store.commit(Commit(items=[msgspec.structs.replace(gone, status="removed", revision=1)]))

    for current in (store, reopen()):
        assert list(current.tracked_items()) == [keep.key]
        assert list(current.removed_items()) == [gone.key]


def test_retracking_a_removed_item_tracks_it_again(store_and_reopen):
    store, reopen = store_and_reopen
    item = tracked("100.000000")
    store.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))
    store.commit(Commit(items=[item]))

    for current in (store, reopen()):
        assert list(current.tracked_items()) == [item.key]
        assert current.removed_items() == {}


def test_archived_and_new_statuses_round_trip_through_a_run_line():
    run = run_record(
        R1,
        T1,
        tracked=[msgspec.structs.replace(tracked("1.0"), archived=True, status="removed")],
        downloads=[download("F1", "gave_up", 3)],
    )
    decoded = run_from_envelope(decode_line(encode_line(run_envelope(run, VERSION))))
    assert decoded == run


def test_run_line_without_archived_decodes_as_not_archived():
    env = run_envelope(run_record(R1, T1, tracked=[tracked("1.0")]), VERSION)
    assert env.object is not None
    del env.object["tracked"][0]["archived"]
    assert run_from_envelope(env).tracked[0].archived is False


def test_downloads_latest_state_across_commits(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(downloads=[download("F1", "pending", 0)]))
    store.commit(Commit(downloads=[download("F1", "failed", 1), download("F2", "pending", 0)]))

    for current in (store, reopen()):
        states = current.downloads()
        assert states[(TEAM, "F1")] == download("F1", "failed", 1)
        assert states[(TEAM, "F2")] == download("F2", "pending", 0)
        assert len(states) == 2


def test_downloads_keep_a_state_until_it_is_settled(store_and_reopen):
    """A directory blob for the target leaves its download state alone; settling deletes it."""
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")], downloads=[download("F1", "pending", 0)]))
    store.append(file_line("F1", T2, R2, "ab" * 32))
    store.commit(Commit())

    for current in (store, reopen()):
        assert current.downloads() == {(TEAM, "F1"): download("F1", "pending", 0)}


def test_append_run_leaves_download_states_to_commits(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(downloads=[download("F1", "pending", 0)]))
    store.drop_downloads(TEAM, {"F1"})
    store.commit(Commit())

    store.append_run(run_record(R1, T1, downloads=[download("F1", "pending", 0)]), VERSION)

    for current in (store, reopen()):
        assert current.downloads() == {}


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
    assert set(reopen().stored_messages(TEAM, CHANNEL)) == {fourth}


def test_drop_directory_lines_removes_every_version(store_and_reopen):
    store, reopen = store_and_reopen
    track(store, TEAM)
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


def test_drop_workspace_removes_raw_and_directory(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "gone", T1, R1))
    store.append(file_line("F1", T1, R1, "ab" * 32))
    store.append(message(TS, "kept", T1, R1, team=OTHER_TEAM))
    store.append(file_line("F2", T1, R1, None, team=OTHER_TEAM))
    store.commit(
        Commit(
            items=[
                tracked("100.000000"),
                TrackedItem(team_id=OTHER_TEAM, kind="conversation", channel=CHANNEL),
            ]
        )
    )
    store.stored_messages(TEAM, CHANNEL)

    store.drop_workspace(TEAM)
    store.commit(Commit())

    for current in (store, reopen()):
        assert current.stored_messages(TEAM, CHANNEL) == {}
        assert current.channels(TEAM) == []
        assert current.directory_state(TEAM, "file") == {}
        assert set(current.stored_messages(OTHER_TEAM, CHANNEL)) == {TS}
        assert list(current.directory_state(OTHER_TEAM, "file")) == ["F2"]
        assert len(current.tracked_items()) == 2

    store.append(message(LATER_TS, "again", T2, R2))
    assert set(reopen().stored_messages(TEAM, CHANNEL)) == {LATER_TS}


def test_blob_references_lists_every_directory_blob(store_and_reopen):
    store, reopen = store_and_reopen
    track(store, TEAM, OTHER_TEAM)
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
    track(store, TEAM)
    store.append(message(TS, "here", T1, R1, channel=OTHER_CHANNEL))
    store.append(message(TS, "there", T1, R1))
    store.append(file_line("F1", T1, R1, None))

    for current in (store, reopen()):
        assert current.channels(TEAM) == [CHANNEL, OTHER_CHANNEL]
        assert current.channels(OTHER_TEAM) == []


def live(store: ArchiveStore, key: tuple[str, str, str, str] = KEY) -> TrackedItem:
    return store.tracked_items()[key]


def test_commit_bumps_revision(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "Hello", T1, R1))

    first = store.commit(Commit(items=[tracked("100.000000")]))
    assert first.revisions == {KEY: 1}
    assert first.dropped == set()
    assert live(store).revision == 1

    second = store.commit(Commit(items=[msgspec.structs.replace(live(store), newest_ts=TS)]))
    assert second.revisions == {KEY: 2}

    expected = msgspec.structs.replace(tracked(TS), revision=2)
    for current in (store, reopen()):
        assert current.tracked_items() == {KEY: expected}
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}


def test_stale_revision_conflicts_and_clears_the_batch(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")]))
    stale = live(store)
    store.commit(Commit(items=[msgspec.structs.replace(stale, newest_ts="200.000000")]))
    store.append(message(TS, "lost", T2, R2))
    store.drop_messages(TEAM, CHANNEL, {TS})
    store.append(message(LATER_TS, "also lost", T2, R2))

    newcomer = tracked("50.000000", OTHER_CHANNEL)
    with pytest.raises(CommitConflict) as caught:
        store.commit(
            Commit(
                items=[newcomer, msgspec.structs.replace(stale, newest_ts="300.000000")],
                downloads=[download("F1", "pending", 0)],
            )
        )
    assert str(caught.value) == "Another slarc command changed the archive at the same time."
    assert store.stored_messages(TEAM, CHANNEL) == {}

    store.commit(Commit())
    for current in (store, reopen()):
        assert current.stored_messages(TEAM, CHANNEL) == {}
        assert list(current.tracked_items()) == [KEY]
        assert (live(current).newest_ts, live(current).revision) == ("200.000000", 2)
        assert current.downloads() == {}


def test_revision_zero_retracks_a_removed_item(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")]))
    store.commit(Commit(items=[msgspec.structs.replace(live(store), status="removed")]))
    assert store.removed_items()[KEY].revision == 2

    committed = store.commit(Commit(items=[tracked("300.000000")]))

    assert committed.revisions == {KEY: 3}
    for current in (store, reopen()):
        assert (live(current).newest_ts, live(current).revision) == ("300.000000", 3)
        assert current.removed_items() == {}


def test_revision_zero_conflicts_with_a_live_item(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")]))

    with pytest.raises(CommitConflict):
        store.commit(Commit(items=[tracked("200.000000")]))

    for current in (store, reopen()):
        assert (live(current).newest_ts, live(current).revision) == ("100.000000", 1)


def test_unchanged_item_conflict(store_and_reopen):
    store, reopen = store_and_reopen
    other_key = (TEAM, "conversation", OTHER_CHANNEL, "")
    store.commit(Commit(items=[tracked("100.000000"), tracked("100.000000", OTHER_CHANNEL)]))
    stale = live(store)
    other = live(store, other_key)
    store.commit(Commit(items=[msgspec.structs.replace(stale, newest_ts="200.000000")]))

    with pytest.raises(CommitConflict):
        store.commit(
            Commit(
                items=[msgspec.structs.replace(other, newest_ts="300.000000")], unchanged=[stale]
            )
        )
    assert live(store, other_key).revision == 1

    committed = store.commit(Commit(items=[other], unchanged=[live(store)]))

    assert committed.revisions == {other_key: 2}
    for current in (store, reopen()):
        assert live(current).revision == 2
        assert live(current, other_key).revision == 2


def test_complete_teams_conflict_when_a_live_item_is_not_named(store_and_reopen):
    store, reopen = store_and_reopen
    kept = TrackedItem(team_id=TEAM, kind="conversation", channel=CHANNEL)
    hidden = TrackedItem(team_id=TEAM, kind="conversation", channel=OTHER_CHANNEL)
    elsewhere = TrackedItem(team_id=OTHER_TEAM, kind="conversation", channel=CHANNEL)
    store.commit(Commit(items=[kept, msgspec.structs.replace(hidden, status="removed")]))
    store.commit(Commit(items=[elsewhere]))
    store.commit(Commit(items=[msgspec.structs.replace(live(store), status="not_visible")]))
    store.append(message(TS, "ok", T1, R1))
    store.commit(Commit(unchanged=[live(store)], complete_teams=frozenset({TEAM})))

    newcomer = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=TS)
    store.commit(Commit(items=[newcomer]))
    store.append(message(LATER_TS, "lost", T2, R2))
    with pytest.raises(CommitConflict):
        store.commit(Commit(unchanged=[live(store)], complete_teams=frozenset({TEAM})))

    for current in (store, reopen()):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}


def test_missing_required_directory_row_conflicts(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")]))
    store.append(message(TS, "lost", T1, R1))

    with pytest.raises(CommitConflict):
        store.commit(Commit(requires={(TEAM, "file", "F1")}))
    assert store.stored_messages(TEAM, CHANNEL) == {}

    store.append(file_line("F1", T1, R1, None))
    store.commit(Commit())
    store.append(message(TS, "kept", T2, R2))
    store.append(file_line("F2", T2, R2, None))
    store.commit(Commit(requires={(TEAM, "file", "F1"), (TEAM, "file", "F2")}))

    with pytest.raises(CommitConflict):
        store.commit(Commit(requires={(TEAM, "image", "F1")}))
    for current in (store, reopen()):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}


def test_directory_rows_need_a_live_item_in_their_team(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(file_line("F1", T1, R1, None))
    with pytest.raises(CommitConflict):
        store.commit(Commit())
    assert store.directory_state(TEAM, "file") == {}

    store.append(file_line("F1", T1, R1, None))
    store.commit(Commit(items=[tracked("100.000000")]))

    store.append(file_line("F2", T1, R1, None, team=OTHER_TEAM))
    with pytest.raises(CommitConflict):
        store.commit(Commit())

    store.append(file_line("F3", T1, R1, None))
    with pytest.raises(CommitConflict):
        store.commit(Commit(items=[msgspec.structs.replace(live(store), status="removed")]))

    store.append(file_line("F4", T1, R1, None))
    store.commit(Commit(items=[msgspec.structs.replace(live(store), status="not_visible")]))

    for current in (store, reopen()):
        assert sorted(current.directory_state(TEAM, "file")) == ["F1", "F4"]
        assert current.directory_state(OTHER_TEAM, "file") == {}


def test_downloads_exist_drops_missing_keys_and_their_directory_rows(store_and_reopen):
    store, reopen = store_and_reopen
    pending = [download("F1", "pending", 0), download("F2", "pending", 0)]
    store.commit(Commit(items=[tracked("100.000000")], downloads=pending))
    store.drop_downloads(TEAM, {"F2"})
    store.commit(Commit())
    store.append(message(TS, "kept", T1, R1))
    store.append(file_line("F1", T1, R1, "ab" * 32))
    store.append(file_line("F2", T1, R1, "cd" * 32))
    store.append(file_line("F3", T1, R1, None))

    committed = store.commit(
        Commit(
            downloads=[download("F3", "failed", 1)],
            settled={(TEAM, "F1"), (TEAM, "F2")},
            downloads_exist=True,
        )
    )

    assert committed.dropped == {(TEAM, "F2"), (TEAM, "F3")}
    for current in (store, reopen()):
        assert current.downloads() == {}
        assert list(current.directory_state(TEAM, "file")) == ["F1"]
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}


def test_drops_wait_for_commit(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "gone", T1, R1))
    store.append(message(LATER_TS, "kept", T1, R1))
    store.append(message(TS, "elsewhere", T1, R1, team=OTHER_TEAM))
    store.append(file_line("F1", T1, R1, None))
    store.commit(Commit(items=[tracked("100.000000")], downloads=[download("F1", "pending", 0)]))

    store.drop_messages(TEAM, CHANNEL, {TS})
    store.drop_directory_lines(TEAM, "file", {"F1"})
    store.drop_downloads(TEAM, {"F1"})
    store.drop_workspace(OTHER_TEAM)

    assert set(store.stored_messages(TEAM, CHANNEL)) == {TS, LATER_TS}
    assert list(store.directory_state(TEAM, "file")) == ["F1"]
    assert list(store.downloads()) == [(TEAM, "F1")]
    assert set(store.stored_messages(OTHER_TEAM, CHANNEL)) == {TS}

    store.commit(Commit())

    for current in (store, reopen()):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {LATER_TS}
        assert current.directory_state(TEAM, "file") == {}
        assert current.downloads() == {}
        assert current.stored_messages(OTHER_TEAM, CHANNEL) == {}


def test_discard_empties_the_batch(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "kept", T1, R1))
    store.commit(Commit(items=[tracked("100.000000")]))
    store.append(message(LATER_TS, "discarded", T2, R2))
    store.drop_messages(TEAM, CHANNEL, {TS})

    store.discard()

    assert set(store.stored_messages(TEAM, CHANNEL)) == {TS}
    store.commit(Commit())
    for current in (store, reopen()):
        assert set(current.stored_messages(TEAM, CHANNEL)) == {TS}


def test_settled_key_deletes_a_download_state_the_same_commit_upserts(store_and_reopen):
    store, reopen = store_and_reopen
    store.commit(Commit(items=[tracked("100.000000")], downloads=[download("F1", "pending", 0)]))

    store.commit(Commit(downloads=[download("F1", "failed", 1)], settled={(TEAM, "F1")}))

    for current in (store, reopen()):
        assert current.downloads() == {}


def test_duplicate_item_keys_in_one_commit_are_rejected(store_and_reopen):
    store, reopen = store_and_reopen
    store.append(message(TS, "kept", T1, R1))

    with pytest.raises(ValueError):
        store.commit(Commit(items=[tracked("100.000000"), tracked("200.000000")]))

    assert set(store.stored_messages(TEAM, CHANNEL)) == {TS}
    for current in (store, reopen()):
        assert current.tracked_items() == {}
