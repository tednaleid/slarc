# ABOUTME: Tests for pulling within a retention: history, backfill and --full start at the cutoff,
# ABOUTME: and reply search finds active threads whose parents are older than it.
from datetime import UTC, datetime, timedelta

import pytest

from slarc import __version__
from slarc.ports import SlarcError
from slarc.service.expiry import expire
from slarc.service.pull import PullOptions
from slarc.unit_of_work import UnitOfWork
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    TEAM,
    Harness,
    channel_messages,
    message,
    ts_on,
)
from tests.service.test_remove import SECOND_CHANNEL, SECOND_LINK
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


def item_retention(h: Harness, channel: str = CHANNEL) -> object:
    return h.archive.tracked_items()[(TEAM, "conversation", channel, "")].retention


def test_a_link_pulled_with_a_retention_keeps_it_for_later_pulls(h: Harness):
    h.pull(BASE, options=PullOptions(retention=90))
    h.pull()

    assert item_retention(h) == 90


def test_a_retention_equal_to_the_workspaces_clears_the_items_own(h: Harness):
    h.set_workspace_retention(30)
    h.pull(BASE, options=PullOptions(retention=90))

    h.pull(BASE, options=PullOptions(retention=30))

    assert item_retention(h) is None


def test_forever_on_an_item_in_a_workspace_without_retention_clears_the_items_own(h: Harness):
    h.pull(BASE, options=PullOptions(retention=90))

    h.pull(BASE, options=PullOptions(retention="forever"))

    assert item_retention(h) is None


def test_a_workspace_name_with_a_retention_sets_the_workspaces(h: Harness):
    h.gateway.conversations[SECOND_CHANNEL] = [message(OCT_18)]
    h.pull(BASE, options=PullOptions(retention=90))
    h.pull(SECOND_LINK)

    h.pull("naleidworkspace", options=PullOptions(retention=30))

    assert h.archive.workspace_settings()[TEAM].retention == 30
    assert item_retention(h) == 90
    assert item_retention(h, SECOND_CHANNEL) is None


def test_a_workspace_name_with_nothing_tracked_is_refused(h: Harness):
    with pytest.raises(SlarcError, match="Nothing is tracked in workspace naleidworkspace"):
        h.pull("naleidworkspace", options=PullOptions(retention=30))

    assert h.archive.workspace_settings() == {}


def test_a_pull_expires_what_is_past_retention_and_says_so(h: Harness):
    h.pull(BASE)
    h.set_workspace_retention(7)

    h.pull()

    assert stored(h) == {OCT_15, OCT_18}
    assert "Expired 2 messages from naleidworkspace." in h.reporter.infos
    assert h.last_run().counts["messages_expired"] == 2


def test_a_pull_that_expires_nothing_says_nothing(h: Harness):
    h.set_workspace_retention(7)
    h.pull(BASE)

    assert not any(info.startswith("Expired") for info in h.reporter.infos)


def test_a_pull_never_downloads_what_it_just_expired(h: Harness):
    h.gateway.conversations[CHANNEL] = channel_messages()
    h.pull(BASE)
    pending = {job.url for job in h.downloader.jobs}
    assert pending
    h.downloader.jobs.clear()
    h.set_workspace_retention(1)
    h.clock.advance(5 * 86400)

    h.pull()

    assert {job.url for job in h.downloader.jobs} & pending <= directory_images(h)


def directory_images(h: Harness) -> set[str]:
    """Avatar and custom emoji images, which belong to the workspace and never expire."""
    avatars = {user["profile"]["image_512"] for user in h.gateway.users_list}
    return avatars | set(h.gateway.emoji_map.values())


def test_a_thread_item_past_its_cutoff_is_not_stored_again(h: Harness):
    h.gateway.conversations[CHANNEL] = [*thread(OCT_1, ts_on(2, 9)), message(OCT_18)]
    h.pull(f"{BASE}/p{OCT_1.replace('.', '')}")
    h.set_workspace_retention(7)
    h.pull()
    assert stored(h) == set()

    h.pull()

    counts = h.last_run().counts
    assert (counts.get("lines_appended", 0), counts.get("messages_expired", 0)) == (0, 0)
