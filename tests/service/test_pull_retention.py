# ABOUTME: Tests for pulling within a retention: history, backfill and --full start at the cutoff,
# ABOUTME: and reply search finds active threads whose parents are older than it.
from datetime import UTC, datetime, timedelta

import pytest

from slarc import __version__
from slarc.service.expiry import expire
from slarc.service.pull import PullOptions
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import BASE, CHANNEL, TEAM, Harness, message, ts_on
from tests.service.test_threads import search_match, thread

START = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)
OCT_1, OCT_10, OCT_15, OCT_18 = ts_on(1, 9), ts_on(10, 9), ts_on(15, 9), ts_on(18, 9)


def cutoff_at(now: datetime, days: int) -> str:
    return f"{(now - timedelta(days=days)).timestamp():.6f}"


def history_calls(h: Harness) -> list[tuple[str | None, ...]]:
    return [call[2:] for call in h.gateway.calls if call[0] == "conversations.history"]


def stored(h: Harness) -> set[str]:
    return set(h.archive.stored_messages(h.tracked()[0].team_id, CHANNEL))


@pytest.fixture
def h() -> Harness:
    h = Harness()
    h.serve([OCT_1, OCT_10, OCT_15, OCT_18])
    return h


def test_a_first_pull_reads_no_history_older_than_the_cutoff(h: Harness):
    h.set_workspace_retention(7)

    h.pull(BASE)

    assert stored(h) == {OCT_15, OCT_18}
    assert history_calls(h) == [(cutoff_at(START, 7), None)]
    [item] = h.tracked()
    assert (item.backfill_complete, item.backfilled_from) == (True, cutoff_at(START, 7))


def test_a_later_pull_of_a_quiet_conversation_reads_from_the_cutoff(h: Harness):
    h.pull(BASE)
    h.set_workspace_retention(7)
    h.gateway.calls.clear()
    h.clock.advance(30 * 86400)

    h.pull()

    assert history_calls(h) == [(cutoff_at(h.clock.now() - timedelta(hours=1), 7), None)]


def test_full_infers_no_deletes_older_than_the_cutoff(h: Harness):
    h.pull(BASE)
    h.set_workspace_retention(7)
    pulled_at = h.clock.now()

    h.pull(options=PullOptions(full=True))

    assert h.lines("deleted") == []
    assert (cutoff_at(pulled_at, 7), None) in history_calls(h)


def test_lengthening_retention_pages_the_gap(h: Harness):
    h.set_workspace_retention(7)
    h.pull(BASE)
    h.set_workspace_retention(30)
    h.gateway.calls.clear()
    pulled_at = h.clock.now()

    h.pull()

    assert stored(h) == {OCT_1, OCT_10, OCT_15, OCT_18}
    assert (cutoff_at(pulled_at, 30), cutoff_at(START, 7)) in history_calls(h)
    [item] = h.tracked()
    assert (item.backfill_complete, item.backfilled_from) == (True, cutoff_at(pulled_at, 30))


def test_lengthening_to_forever_pages_the_gap(h: Harness):
    h.set_workspace_retention(7)
    h.pull(BASE)
    h.set_workspace_retention(None)

    h.pull()

    assert stored(h) == {OCT_1, OCT_10, OCT_15, OCT_18}


def test_the_daily_move_of_the_cutoff_never_reopens_the_backfill(h: Harness):
    h.set_workspace_retention(7)
    h.pull(BASE)
    h.gateway.calls.clear()
    h.clock.advance(2 * 86400)

    h.pull()

    assert all(latest is None for _, latest in history_calls(h))


def test_a_first_pull_finds_an_active_thread_with_an_older_parent(h: Harness):
    parent, reply = OCT_1, ts_on(18, 14)
    h.gateway.conversations[CHANNEL] = [*thread(parent, reply), message(OCT_18)]
    h.gateway.search_results[f"in:<#{CHANNEL}> after:2026-10-10"] = [search_match(reply, parent)]
    h.set_workspace_retention(7)

    h.pull(BASE)

    assert stored(h) == {parent, reply, OCT_18}
    assert h.last_run().counts["threads_read"] == {"search": 1}


def test_full_with_retention_finds_an_active_thread_with_an_older_parent(h: Harness):
    parent, reply = OCT_1, ts_on(18, 14)
    h.gateway.conversations[CHANNEL] = [*thread(parent, reply), message(OCT_18)]
    h.set_workspace_retention(7)
    h.pull(BASE)
    h.gateway.calls.clear()
    pulled_at = h.clock.now()
    query = f"in:<#{CHANNEL}> after:{(pulled_at - timedelta(days=9)).strftime('%Y-%m-%d')}"
    h.gateway.search_results[query] = [search_match(reply, parent)]

    h.pull(options=PullOptions(full=True))

    assert ("search.messages", query) in h.gateway.calls
    assert parent in stored(h)


def test_a_search_hit_on_an_expired_thread_rereads_it_whole(h: Harness):
    parent, old_reply = OCT_1, ts_on(2, 9)
    h.gateway.conversations[CHANNEL] = [*thread(parent, old_reply), message(OCT_18)]
    h.set_workspace_retention(7)
    h.pull(BASE)
    assert stored(h) == {OCT_18}
    new_reply = ts_on(19, 14)
    h.gateway.conversations[CHANNEL].append(message(new_reply, thread_ts=parent))
    h.gateway.search_results[f"in:<#{CHANNEL}> after:2026-10-17"] = [
        search_match(new_reply, parent)
    ]

    h.pull()

    assert stored(h) == {parent, old_reply, new_reply, OCT_18}


def test_later_searches_never_reach_further_back_than_two_days_before_the_cutoff(h: Harness):
    h.set_workspace_retention(1)
    h.pull(BASE)
    h.clock.advance(10 * 86400)
    h.gateway.calls.clear()
    pulled_at = h.clock.now()

    h.pull()

    [query] = [call[1] for call in h.gateway.calls if call[0] == "search.messages"]
    expected = pulled_at - timedelta(days=3)
    assert query == f"in:<#{CHANNEL}> after:{expected.strftime('%Y-%m-%d')}"


def test_lengthening_after_expiry_rereads_what_expiry_removed(h: Harness):
    h.pull(BASE)
    h.set_workspace_retention(7)
    h.pull()
    uow = UnitOfWork(h.archive, h.blobs, h.clock, "pull", [], __version__)
    with uow:
        expire(uow, [TEAM], h.blobs, h.reporter)
    assert stored(h) == {OCT_15, OCT_18}
    h.set_workspace_retention(None)

    h.pull()

    assert stored(h) == {OCT_1, OCT_10, OCT_15, OCT_18}
