# ABOUTME: Tests for the listen loop over fakes: a catch-up pull first, events stored in segments,
# ABOUTME: catch-up pulls after a lost connection, hour-long segments and rejected credentials.
from collections.abc import Callable

import msgspec
import pytest

from slarc import __version__
from slarc.domain.tracking import RunRecord, TrackedItem, run_from_envelope
from slarc.ports import AuthFailed, Commit, ConnectionLost, SlarcError
from slarc.service import listen as listen_module
from slarc.service.listen import ListenOptions, listen, unheard_items
from slarc.unit_of_work import UnitOfWork
from tests.fakes import FakeEventConnection, FakeEventConnector
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    OTHER,
    OTHER_CHANNEL,
    OTHER_TEAM,
    PARENT_TS,
    TEAM,
    Harness,
    message,
    other_gateway,
)

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
    assert h.reporter.infos[2] == "Listening to naleidworkspace (1 tracked item)."


def test_listen_says_what_it_pulls_before_it_listens(h: Harness):
    run_listen(h, FakeEventConnector(), ticks(h, 5))

    assert h.reporter.infos[:3] == [
        "Pulling 1 tracked item before listening.",
        "Pulled 1 tracked item: 0 new messages.",
        "Listening to naleidworkspace (1 tracked item).",
    ]
    assert h.reporter.pulls == ["started", "ended"]


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
    assert h.reporter.infos[2] == (
        "Listening to naleidworkspace (1 tracked item; 1 not heard, pulled every 15 minutes)."
    )


def test_an_unheard_pull_shows_no_rows_and_prints_only_new_messages(h: Harness):
    h.set_member(False)
    h.pull()
    h.gateway.calls.clear()

    run_listen(h, FakeEventConnector(), ticks(h, 20 * 60))

    assert history_reads(h) == 2
    assert h.reporter.pulls == ["started", "ended"]
    assert not [line for line in h.reporter.infos if "not heard:" in line]


def remove_tracked(h: Harness) -> None:
    """Commits the removal of the one tracked item, as an rm in another process would."""
    with h.archive.elsewhere() as archive:
        [item] = archive.tracked_items().values()
        archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))


def at_call(h: Harness, count: int, call: int, action: Callable[[], None]) -> Callable[[], bool]:
    """ticks(h, count) that also runs action on the given stop check."""
    base = ticks(h, count)
    calls = [0]

    def stop() -> bool:
        calls[0] += 1
        if calls[0] == call:
            action()
        return base()

    return stop


def test_listen_connects_before_its_first_pull(h: Harness):
    events = FakeEventConnector()
    opened_at_first_read: list[list[str]] = []
    h.gateway.before_history_page(1, lambda: opened_at_first_read.append(list(events.opened)))

    run_listen(h, events, ticks(h, 3))

    assert opened_at_first_read == [[TEAM]]


def test_bare_listen_with_rejected_credentials_raises(h: Harness):
    h.gateway.auth_error = AuthFailed("desktop", TEAM)

    with pytest.raises(AuthFailed):
        run_listen(h, FakeEventConnector(), ticks(h, 5))


def test_a_catch_up_that_cannot_reach_slack_is_tried_again_later(h: Harness):
    dropped = FakeEventConnection([ConnectionLost("closed")])
    events = FakeEventConnector({TEAM: [dropped, FakeEventConnection()]})

    def offline() -> None:
        h.gateway.auth_error = SlarcError("Could not reach Slack to call auth.test.")

    def online() -> None:
        h.gateway.auth_error = None

    stop = at_call(h, 200, 2, offline)
    late = at_call(h, 200, 100, online)

    run_listen(h, events, lambda: stop() or late())

    assert history_reads(h) == 2
    assert any("Could not reach Slack" in warning for warning in h.reporter.warnings)


def test_rm_of_an_unheard_item_stays_removed(h: Harness):
    h.set_member(False)
    h.pull()

    run_listen(h, FakeEventConnector(), at_call(h, 20 * 60, 60, lambda: remove_tracked(h)))

    assert h.archive.tracked_items() == {}


def test_rm_of_a_workspace_then_a_profile_change_keeps_listening(h: Harness):
    connection = FakeEventConnection()
    user = {"id": "U0NEWUSER1", "name": "ada", "real_name": "Ada", "profile": {}}

    def rm_then_profile_change() -> None:
        remove_tracked(h)
        connection.events.append({"type": "user_change", "user": user})

    events = FakeEventConnector({TEAM: [connection]})
    run_listen(h, events, at_call(h, 60, 5, rm_then_profile_change))

    assert not [env for env in h.lines("user") if env.meta.id == "U0NEWUSER1"]


def test_a_stop_skips_the_catch_up_a_lost_connection_asked_for(h: Harness):
    events = FakeEventConnector({TEAM: [FakeEventConnection([ConnectionLost("closed")])]})

    def reconnected() -> bool:
        h.clock.advance(1)
        return len(events.opened) >= 2

    run_listen(h, events, reconnected)

    assert history_reads(h) == 1


def test_a_segment_reads_the_directory_once_for_many_batches(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    reads: list[str] = []
    stored_view = listen_module._stored_view

    def counted(uow: UnitOfWork, team_id: str):
        reads.append(team_id)
        return stored_view(uow, team_id)

    monkeypatch.setattr(listen_module, "_stored_view", counted)
    gap: list[dict | None] = [None, None, None]
    spaced = [event("1792500001.000100"), *gap, event("1792500002.000100"), *gap]
    spaced.append(event("1792500003.000100"))
    events = FakeEventConnector({TEAM: [FakeEventConnection(spaced)]})

    run_listen(h, events, ticks(h, 30))

    assert len([line for line in h.reporter.infos if "message from" in line]) == 3
    assert reads == [TEAM]


THREAD = TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT_TS)


def track_elsewhere(h: Harness, item: TrackedItem) -> None:
    """Commits a tracked item, as a pull in another process would."""
    with h.archive.elsewhere() as archive:
        archive.commit(Commit(items=[item]))


def test_an_item_tracked_while_listening_is_named_within_seconds(h: Harness):
    run_listen(h, FakeEventConnector(), at_call(h, 12, 3, lambda: track_elsewhere(h, THREAD)))

    assert f"Now also listening to thread {PARENT_TS} in #all-naleid." in h.reporter.infos


def test_a_workspace_tracked_while_listening_is_connected_then_pulled(h: Harness):
    other = other_gateway()
    h.connector.gateways[OTHER] = other
    item = TrackedItem(team_id=OTHER_TEAM, kind="conversation", channel=OTHER_CHANNEL)
    events = FakeEventConnector()

    run_listen(h, events, at_call(h, 30, 3, lambda: track_elsewhere(h, item)))

    assert events.opened == [TEAM, OTHER_TEAM]
    assert any(call[0] == "conversations.history" for call in other.calls)
    assert f"Now also listening to {OTHER_CHANNEL}." in h.reporter.infos


def test_an_unheard_item_tracked_while_listening_is_pulled_with_the_others(h: Harness):
    h.set_member(False)
    h.pull()

    run_listen(h, FakeEventConnector(), at_call(h, 12, 3, lambda: track_elsewhere(h, THREAD)))

    assert (
        f"Now also pulling thread {PARENT_TS} in #all-naleid every 15 minutes; "
        "listen cannot hear it."
    ) in h.reporter.infos


def test_an_item_removed_while_listening_is_named(h: Harness):
    track_elsewhere(h, THREAD)

    def remove_thread() -> None:
        with h.archive.elsewhere() as archive:
            item = archive.tracked_items()[THREAD.key]
            archive.commit(Commit(items=[msgspec.structs.replace(item, status="removed")]))

    run_listen(h, FakeEventConnector(), at_call(h, 12, 3, remove_thread))

    assert f"No longer listening to thread {PARENT_TS} in #all-naleid." in h.reporter.infos


def test_a_workspace_with_nothing_tracked_any_more_is_dropped_within_seconds(h: Harness):
    events = FakeEventConnector()

    run_listen(h, events, at_call(h, 12, 3, lambda: remove_tracked(h)))

    assert "naleidworkspace  nothing tracked any more; stopped listening" in h.reporter.infos
    assert history_reads(h) == 1


def test_each_hour_of_listening_expires_what_is_past_retention(h: Harness):
    h.clock.advance(3 * 86400)
    base = ticks(h, 70, seconds=60)
    calls = [0]

    def stop() -> bool:
        calls[0] += 1
        if calls[0] == 3:
            with h.archive.elsewhere():
                h.set_workspace_retention(1)
        return base()

    run_listen(h, FakeEventConnector(), stop)

    assert history_reads(h) == 1
    assert h.archive.stored_messages(TEAM, CHANNEL) == {}
    assert "Expired 9 messages and 4 files from naleidworkspace." in h.reporter.infos
    listen_runs = [run for run in listen_records(h) if run.command == "listen"]
    assert any(run.counts.get("messages_expired") for run in listen_runs)


def listen_records(h: Harness) -> list[RunRecord]:
    return [run_from_envelope(env) for env in h.lines("run")]
