# ABOUTME: Tests for the listen loop over fakes: a catch-up pull first, events stored in segments,
# ABOUTME: catch-up pulls after a lost connection, hour-long segments and rejected credentials.
from collections.abc import Callable

import pytest

from slarc import __version__
from slarc.domain.tracking import TrackedItem
from slarc.ports import AuthFailed, ConnectionLost
from slarc.service.listen import ListenOptions, listen, unheard_items
from slarc.unit_of_work import UnitOfWork
from tests.fakes import FakeEventConnection, FakeEventConnector
from tests.service.pull_harness import BASE, CHANNEL, PARENT_TS, TEAM, Harness, message

NEW_TS = "1792500000.000100"
OPTIONS = ListenOptions(downloads=4, max_file_bytes=100 * 1024 * 1024)


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.pull(BASE)
    harness.gateway.calls.clear()
    return harness


def event(ts: str, **extra: object) -> dict:
    return {"type": "message", "channel": CHANNEL, "event_ts": ts, **message(ts, **extra)}


def ticks(h: Harness, count: int, seconds: float = 1.0) -> Callable[[], bool]:
    """A stop check that advances the clock each call and stops after count calls."""
    left = [count]

    def stop() -> bool:
        h.clock.advance(seconds)
        left[0] -= 1
        return left[0] < 0

    return stop


def run_listen(h: Harness, events: FakeEventConnector, stop: Callable[[], bool]) -> None:
    def open_uow(command: str) -> UnitOfWork:
        return UnitOfWork(h.archive, h.blobs, h.clock, command, [], __version__)

    listen([], OPTIONS, open_uow, h.connector, events, h.downloader, h.reporter, h.clock, stop)


def history_reads(h: Harness) -> int:
    return sum(1 for call in h.gateway.calls if call[0] == "conversations.history")


def runs(h: Harness) -> list[str]:
    return [env.object["command"] for env in h.lines("run") if env.object]


def test_listen_pulls_first_then_stores_events(h: Harness):
    events = FakeEventConnector({TEAM: [FakeEventConnection([event(NEW_TS, text="hello")])]})

    run_listen(h, events, ticks(h, 15))

    assert history_reads(h) == 1
    [line] = [env for env in h.archive.lines if env.meta.ts == NEW_TS]
    assert line.meta.api == "rtm"
    assert "#all-naleid  new message from Ted Naleid" in h.reporter.infos
    assert h.reporter.infos[0] == "Listening to naleidworkspace (1 tracked item)."


def test_events_already_stored_by_a_pull_add_nothing(h: Harness):
    [stored] = [m for m in h.gateway.conversations[CHANNEL] if m["ts"] == PARENT_TS]
    pushed = {"type": "message", "channel": CHANNEL, "event_ts": PARENT_TS, **stored}
    events = FakeEventConnector({TEAM: [FakeEventConnection([pushed])]})
    before = [env for env in h.archive.lines if env.meta.ts == PARENT_TS]

    run_listen(h, events, ticks(h, 15))

    assert [env for env in h.archive.lines if env.meta.ts == PARENT_TS] == before
    assert not [line for line in h.reporter.infos if "message from" in line]


def test_unplanned_reconnect_runs_a_catch_up_pull(h: Harness):
    dropped = FakeEventConnection([ConnectionLost("closed")])
    events = FakeEventConnector({TEAM: [dropped, FakeEventConnection()]})

    run_listen(h, events, ticks(h, 20))

    assert events.opened == [TEAM, TEAM]
    assert history_reads(h) == 2


def test_segment_ends_after_an_hour_and_writes_a_run_row(h: Harness):
    before = len(runs(h))

    run_listen(h, FakeEventConnector(), ticks(h, 70, seconds=60))

    assert runs(h)[before:] == ["listen", "listen", "listen"]


def test_rejected_credentials_stop_listen(h: Harness):
    events = FakeEventConnector({TEAM: [AuthFailed("desktop", TEAM)]})

    with pytest.raises(AuthFailed):
        run_listen(h, events, ticks(h, 15))

    assert runs(h)[-1] == "listen"


def test_unheard_items_are_channels_not_joined_and_their_threads():
    joined = TrackedItem(team_id=TEAM, kind="conversation", channel="C0JOINED01")
    not_joined = TrackedItem(team_id=TEAM, kind="conversation", channel="C0NOTJOIN1")
    thread = TrackedItem(team_id=TEAM, kind="thread", channel="C0NOTJOIN1", ts=PARENT_TS)
    dm = TrackedItem(team_id=TEAM, kind="conversation", channel="D0DIRECT01")
    unknown = TrackedItem(team_id=TEAM, kind="conversation", channel="C0UNKNOWN1")
    conversations = {
        "C0JOINED01": {"id": "C0JOINED01", "is_member": True},
        "C0NOTJOIN1": {"id": "C0NOTJOIN1", "is_member": False},
        "D0DIRECT01": {"id": "D0DIRECT01", "is_im": True},
    }

    found = unheard_items([joined, not_joined, thread, dm, unknown], conversations)

    assert found == [not_joined, thread]


def test_unheard_items_are_pulled_every_fifteen_minutes(h: Harness):
    h.set_member(False)
    h.pull()
    h.gateway.calls.clear()

    run_listen(h, FakeEventConnector(), ticks(h, 20 * 60))

    assert history_reads(h) == 2
    assert h.reporter.infos[0] == (
        "Listening to naleidworkspace (1 tracked item; 1 not heard, pulled every 15 minutes)."
    )
