# ABOUTME: Tests for the unit of work that scopes one run over the archive and blob stores.
# ABOUTME: The run line must be appended however the run ends: success, exception or Ctrl-C.
from datetime import UTC, datetime

import pytest

from slarc.unit_of_work import UnitOfWork
from tests.fakes import FakeClock, InMemoryArchiveStore, InMemoryBlobStore

START = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)


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
