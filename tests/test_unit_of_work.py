# ABOUTME: Tests for the unit of work that scopes one run over the archive and blob stores.
# ABOUTME: Covers commits that thread revisions and download states, and the run line written
# ABOUTME: however the run ends: success, exception or Ctrl-C.
from datetime import UTC, datetime
from typing import Literal

import pytest

from slarc.domain.envelope import Envelope, Meta
from slarc.domain.tracking import DownloadState, TrackedItem
from slarc.ports import Commit, CommitConflict, Committed
from slarc.unit_of_work import UnitOfWork
from tests.fakes import FakeClock, InMemoryArchiveStore, InMemoryBlobStore

START = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)
TEAM = "T0CB5LYPVFA"
CHANNEL = "C0CA5396G03"
OTHER_CHANNEL = "C0OTHER0002"
TS = "1792410693.663869"
FETCHED = "2026-10-19T13:40:11Z"
RUN = "r_20261019T134011Z"
ITEM = TrackedItem(team_id=TEAM, kind="conversation", channel=CHANNEL)
OTHER_ITEM = TrackedItem(team_id=TEAM, kind="conversation", channel=OTHER_CHANNEL)


def make_uow() -> tuple[UnitOfWork, InMemoryArchiveStore, FakeClock]:
    archive = InMemoryArchiveStore()
    clock = FakeClock(START)
    uow = UnitOfWork(
        archive=archive,
        blobs=InMemoryBlobStore(),
        clock=clock,
        command="pull",
        args=["#general"],
        slarc_version="0.1.0",
    )
    return uow, archive, clock


def run_lines(archive: InMemoryArchiveStore) -> list:
    return [env for env in archive.lines if env.meta.kind == "run"]


def test_run_line_written_on_success():
    uow, archive, clock = make_uow()
    with uow:
        assert uow.run.run == "r_20261019T134011Z"
        assert uow.run.started == "2026-10-19T13:40:11Z"
        assert uow.archive is archive
        assert run_lines(archive) == []
        clock.advance(65)

    [line] = run_lines(archive)
    assert line.meta.slarc == "0.1.0"
    assert line.object is not None
    assert line.object["command"] == "pull"
    assert line.object["args"] == ["#general"]
    assert line.object["finished"] == "2026-10-19T13:41:16Z"


def test_run_line_written_on_exception():
    uow, archive, clock = make_uow()
    with pytest.raises(RuntimeError), uow:
        clock.advance(5)
        raise RuntimeError("boom")

    [line] = run_lines(archive)
    assert line.object is not None
    assert line.object["finished"] == "2026-10-19T13:40:16Z"


def test_run_line_written_on_keyboard_interrupt():
    uow, archive, _ = make_uow()
    with pytest.raises(KeyboardInterrupt), uow:
        uow.run.counts["messages_seen"] = 3
        raise KeyboardInterrupt

    [line] = run_lines(archive)
    assert line.object is not None
    assert line.object["counts"] == {"messages_seen": 3}
    assert line.object["finished"] == "2026-10-19T13:40:11Z"


def test_run_id_carries_the_run_suffix():
    archive = InMemoryArchiveStore()
    uow = UnitOfWork(archive, InMemoryBlobStore(), FakeClock(START), "pull", [], "0.1.0", "3f9a")
    with uow:
        assert uow.run.run == "r_20261019T134011Z_3f9a"


class RecordingArchiveStore(InMemoryArchiveStore):
    """Keeps every Commit it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: list[Commit] = []

    def commit(self, change: Commit) -> Committed:
        self.commits.append(change)
        return super().commit(change)


def recording_uow() -> tuple[UnitOfWork, RecordingArchiveStore]:
    archive = RecordingArchiveStore()
    uow = UnitOfWork(archive, InMemoryBlobStore(), FakeClock(START), "pull", [], "0.1.0")
    return uow, archive


def download(target: str, status: Literal["pending", "failed"] = "pending") -> DownloadState:
    return DownloadState(team_id=TEAM, target_kind="file", target=target, status=status)


def file_line(file_id: str) -> Envelope:
    meta = Meta(kind="file", team=TEAM, id=file_id, fetched_at=FETCHED, run=RUN, hash="h1")
    return Envelope(meta=meta, object={"id": file_id})


def message_line(ts: str) -> Envelope:
    meta = Meta(
        kind="message", team=TEAM, channel=CHANNEL, ts=ts, fetched_at=FETCHED, run=RUN, hash="h2"
    )
    return Envelope(meta=meta, object={"ts": ts})


def test_commit_threads_revisions_through_stale_copies():
    uow, archive = recording_uow()
    with uow:
        uow.commit([ITEM])
        uow.commit([ITEM])
        uow.commit(unchanged=[ITEM])

    assert [change.items[0].revision for change in archive.commits[:2]] == [0, 1]
    assert archive.commits[2].unchanged[0].revision == 2
    assert archive.tracked[ITEM.key].revision == 2


def test_commit_sends_changed_download_states_only():
    uow, archive = recording_uow()
    with uow:
        uow.run.downloads.extend([download("F1"), download("F2")])
        uow.commit([ITEM])
        archive.append(file_line("F1"))
        uow.commit()
        uow.run.downloads[1] = download("F2", "failed")
        uow.settle((TEAM, "F3"))
        uow.require((TEAM, "file", "F1"))
        uow.commit()

    first, second, third, left_open = archive.commits
    assert first.downloads == [download("F1"), download("F2")]
    assert (second.downloads, second.settled, second.requires) == ([], set(), set())
    assert third.downloads == [download("F2", "failed")]
    assert (third.settled, third.requires) == ({(TEAM, "F3")}, {(TEAM, "file", "F1")})
    assert (left_open.downloads, left_open.settled, left_open.requires) == ([], set(), set())


def test_commit_forgets_and_returns_dropped_download_states():
    uow, archive = recording_uow()
    with uow:
        uow.run.downloads.append(download("F1"))
        assert uow.commit(downloads_exist=True) == {(TEAM, "F1")}

        assert uow.run.downloads == []
        uow.run.downloads.append(download("F1"))
        uow.commit()

    assert archive.commits[0].downloads_exist is True
    assert archive.commits[1].downloads == [download("F1")]


def test_conflict_restores_downloads_and_reraises():
    uow, archive = recording_uow()
    with uow:
        uow.run.downloads.append(download("F1"))
        uow.commit([ITEM])
        archive.commit(Commit(items=[archive.tracked_items()[ITEM.key]]))
        uow.run.downloads[0] = download("F1", "failed")
        uow.run.downloads.append(download("F2"))
        uow.settle((TEAM, "F1"))
        uow.require((TEAM, "file", "F9"))
        archive.append(message_line(TS))

        with pytest.raises(CommitConflict):
            uow.commit([ITEM])

        assert uow.run.downloads == [download("F1")]
        assert archive.stored_messages(TEAM, CHANNEL) == {}
        uow.commit()

    retried = archive.commits[-2]
    assert (retried.downloads, retried.settled, retried.requires) == ([], set(), set())
    assert archive.downloads() == {(TEAM, "F1"): download("F1")}


def test_exit_after_an_exception_discards_the_open_batch_then_writes_the_run_row():
    uow, archive = recording_uow()
    with pytest.raises(RuntimeError), uow:
        uow.run.downloads.append(download("F1"))
        uow.commit([ITEM])
        archive.append(message_line(TS))
        uow.run.tracked.append(OTHER_ITEM)
        uow.run.downloads.append(download("F2"))
        raise RuntimeError("boom")

    assert len(archive.commits) == 1
    assert archive.stored_messages(TEAM, CHANNEL) == {}
    assert list(archive.tracked_items()) == [ITEM.key]
    assert archive.downloads() == {(TEAM, "F1"): download("F1")}
    assert archive.lines[-1].meta.kind == "run"


def test_exit_commits_what_is_left_open():
    uow, archive = recording_uow()
    with uow:
        archive.append(file_line("F1"))
        archive.append(message_line(TS))
        uow.run.tracked.append(ITEM)
        uow.run.downloads.append(download("F1"))

    [left_open] = archive.commits
    assert left_open.items == [ITEM]
    assert left_open.downloads == [download("F1")]
    assert archive.tracked_items()[ITEM.key].revision == 1
    assert list(archive.directory_state(TEAM, "file")) == ["F1"]
    assert set(archive.stored_messages(TEAM, CHANNEL)) == {TS}
    assert archive.lines[-1].meta.kind == "run"


class FailingArchiveStore(InMemoryArchiveStore):
    """Raises the given error from every commit, writing nothing."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def commit(self, change: Commit) -> Committed:
        raise self.error


@pytest.mark.parametrize("error", [RuntimeError("disk full"), KeyboardInterrupt()])
def test_exit_commit_failure_discards_writes_the_run_row_and_raises(error: BaseException):
    archive = FailingArchiveStore(error)
    uow = UnitOfWork(archive, InMemoryBlobStore(), FakeClock(START), "pull", [], "0.1.0")
    with pytest.raises(type(error)), uow:
        archive.append(message_line(TS))

    assert archive.stored_messages(TEAM, CHANNEL) == {}
    assert len(run_lines(archive)) == 1


def test_exit_records_a_conflict_in_the_run_errors():
    uow, archive = recording_uow()
    archive.commit(Commit(items=[ITEM]))
    with uow:
        archive.append(message_line(TS))
        uow.run.tracked.append(ITEM)

    assert archive.stored_messages(TEAM, CHANNEL) == {}
    [line] = run_lines(archive)
    assert line.object is not None
    assert line.object["errors"] == [
        {"error": "Another slarc command changed the archive at the same time."}
    ]
