# ABOUTME: Tests for pulling whole conversations: date ranges, paging, backfill and thread reads.
# ABOUTME: A fake gateway pages history newest first; some tests interrupt or observe its paging.
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest

from slarc.domain.envelope import Envelope
from slarc.ports import ArchiveWriteFailed, AuthFailed, NotVisible, SlarcError, ThreadNotFound
from slarc.service.pull import PullOptions, date_to_ts
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    NALEID,
    OTHER,
    OTHER_CHANNEL,
    OTHER_LINK,
    PARENT_TS,
    REPLY_TS,
    TEAM,
    THREAD_TS,
    Harness,
    channel_messages,
    message,
    other_gateway,
    spike,
    ts_on,
)

BROADCAST_TS = "1792422700.000100"
SECOND_CHANNEL = "C0SECOND001"
SECOND_LINK = f"https://naleidworkspace.slack.com/archives/{SECOND_CHANNEL}"
UNREACHABLE = "Could not reach Slack to call conversations.replies."


def history_calls(h: Harness) -> list[tuple[str | None, ...]]:
    """The (oldest, latest) bounds of each history page served."""
    return [call[2:] for call in h.gateway.calls if call[0] == "conversations.history"]


def message_ts_since(h: Harness, before: int) -> list[str]:
    """The ts of each message line appended after the first `before` lines, in order."""
    lines = h.archive.lines[before:]
    return [env.meta.ts for env in lines if env.meta.kind == "message" and env.meta.ts]


@pytest.fixture
def h() -> Harness:
    return Harness()


def test_first_pull_pages_whole_range(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(7)]
    h.serve(stamps, page_size=3)

    report = h.pull(BASE)

    assert message_ts_since(h, 0) == stamps[::-1]
    assert {env.meta.api for env in h.lines("message")} == {"conversations.history"}
    assert history_calls(h) == [(None, None)] * 3
    assert report.messages_seen == 7
    [item] = h.tracked()
    assert (item.kind, item.channel, item.ts, item.status) == ("conversation", CHANNEL, None, "ok")
    assert (item.since, item.until) == (None, None)
    assert (item.newest_ts, item.oldest_ts, item.backfill_complete) == (stamps[-1], stamps[0], True)
    assert item.last_pull_at == "2026-10-19T13:40:11Z"
    assert item.last_read == {ME: spike("conversations.info")["channel"]["last_read"]}


def test_thread_parent_read_in_history_and_replies_counts_once(h: Harness):
    report = h.pull(BASE)

    unique = {m["ts"] for m in channel_messages()}
    assert report.messages_seen == len(unique)
    assert h.last_run().counts["messages_seen"] == len(unique)
    label, read, _, total = h.reporter.progress_updates[-1]
    assert (label, read, total) == ("#all-naleid", len(unique), len(unique))


def test_report_counts_message_and_deleted_lines_as_new_messages(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(3)]
    h.serve(stamps)
    first = h.pull(BASE)
    h.change_message(stamps[1], text="edited")
    h.remove_messages(stamps[2])

    second = h.pull(options=PullOptions(full=True))

    assert first.messages_new == 3
    assert second.messages_new == 2
    assert first.lines_appended > first.messages_new


def test_progress_reported_per_page_and_item(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(7)]
    h.serve(stamps, page_size=3)

    report = h.pull(BASE)

    assert h.reporter.progress_updates == [
        ("#all-naleid", 0, 0, None),
        ("#all-naleid", 3, 3, None),
        ("#all-naleid", 6, 6, None),
        ("#all-naleid", 7, 7, None),
        ("#all-naleid", 7, 7, 7),
    ]
    assert report.items_pulled == 1


def test_progress_reported_after_each_thread_under_a_page(h: Harness):
    first, second = ts_on(18, 1), ts_on(18, 3)
    h.gateway.conversations[CHANNEL] = [
        message(first, thread_ts=first, reply_count=1),
        message(ts_on(18, 2), thread_ts=first),
        message(second, thread_ts=second, reply_count=1),
        message(ts_on(18, 4), thread_ts=second),
    ]

    h.pull(BASE)

    assert h.reporter.progress_updates == [
        ("#all-naleid", 0, 0, None),
        ("#all-naleid", 2, 2, None),
        ("#all-naleid", 3, 3, None),
        ("#all-naleid", 4, 4, None),
        ("#all-naleid", 4, 4, 4),
    ]


def test_progress_counts_only_changed_messages_as_new(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(3)]
    h.serve(stamps)
    h.pull(BASE)
    h.change_message(stamps[1], text="edited")
    h.reporter.progress_updates.clear()

    h.pull(options=PullOptions(full=True))

    assert h.reporter.progress_updates[-1] == ("#all-naleid", 3, 1, 3)


def test_since_until_bound_history(h: Harness):
    stamps = {day: ts_on(day, 12) for day in (17, 18, 19, 20)}
    h.serve(list(stamps.values()))

    h.pull(BASE, options=PullOptions(since="2026-10-18", until="2026-10-19"))

    assert message_ts_since(h, 0) == [stamps[19], stamps[18]]
    assert date_to_ts("2026-10-18", end_of_day=False) == "1792281600.000000"
    assert date_to_ts("2026-10-19", end_of_day=True) == "1792454400.000000"
    assert history_calls(h) == [("1792281600.000000", "1792454400.000000")]
    [item] = h.tracked()
    assert (item.since, item.until) == ("1792281600.000000", "1792454400.000000")


def test_threads_in_range_are_read(h: Harness):
    old_parent = ts_on(17, 1)
    h.gateway.conversations[CHANNEL] += [
        message(old_parent, thread_ts=old_parent, reply_count=1),
        message(ts_on(17, 2), thread_ts=old_parent),
    ]

    h.pull(BASE, options=PullOptions(since="2026-10-18"))

    replies = [call for call in h.gateway.calls if call[0] == "conversations.replies"]
    assert replies == [("conversations.replies", CHANNEL, PARENT_TS)]
    apis = {env.meta.ts: env.meta.api for env in h.lines("message")}
    assert len(h.lines("message")) == len(apis) == len(channel_messages())
    assert {ts: apis[ts] for ts in THREAD_TS} == {
        PARENT_TS: "conversations.history",
        THREAD_TS[1]: "conversations.replies",
        THREAD_TS[2]: "conversations.replies",
        THREAD_TS[3]: "conversations.replies",
    }


def test_broadcast_reply_stored_once(h: Harness):
    broadcast = message(BROADCAST_TS, thread_ts=PARENT_TS, subtype="thread_broadcast")
    h.gateway.conversations[CHANNEL].append(broadcast)

    h.pull(BASE)

    [line] = [env for env in h.lines("message") if env.meta.ts == BROADCAST_TS]
    assert line.object == broadcast
    assert ("conversations.replies", CHANNEL, PARENT_TS) in h.gateway.calls


def test_incremental_pull_fetches_only_newer(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(3)]
    h.serve(stamps)
    h.pull(BASE)
    newer = [ts_on(19, hour) for hour in range(2)]
    h.gateway.conversations[CHANNEL] += [message(ts) for ts in newer]
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    report = h.pull()

    assert message_ts_since(h, before) == newer[::-1]
    assert history_calls(h) == [(stamps[-1], None)]
    assert report.messages_seen == 2
    [item] = h.tracked()
    assert (item.newest_ts, item.oldest_ts, item.backfill_complete) == (newer[-1], stamps[0], True)


def test_run_line_records_rate_limit_waits_to_milliseconds(h: Harness):
    h.gateway.rate_limit_wait_s = 7.4999998734

    h.pull(BASE)

    assert h.last_run().counts["rate_limit_wait_s"] == 7.5


def test_interrupted_run_line_records_rate_limit_waits(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.gateway.rate_limit_wait_s = 1.25

    def interrupted(channel: str, oldest: str | None, latest: str | None) -> Iterator[list[dict]]:
        raise KeyboardInterrupt
        yield []

    monkeypatch.setattr(h.gateway, "history", interrupted)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)

    assert h.last_run().counts["rate_limit_wait_s"] == 1.25


def test_run_line_counts_api_calls_per_method(h: Harness):
    h.serve([ts_on(18, hour) for hour in range(3)], page_size=2)

    h.pull(BASE)

    assert h.last_run().counts["api_calls"] == {
        "auth.test": 1,
        "conversations.history": 2,
        "conversations.info": 1,
        "emoji.list": 1,
        "usergroups.list": 1,
        "users.list": 1,
    }


def test_interrupted_run_line_records_api_calls_and_thread_reads(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    parent, reply, older = ts_on(18, 5), ts_on(18, 6), ts_on(18, 1)
    h.gateway.conversations[CHANNEL] = [
        message(parent, thread_ts=parent, reply_count=1),
        message(reply, thread_ts=parent),
        message(older),
    ]
    h.gateway.page_size = 1
    served = h.gateway.history

    def interrupted_on_page_2(
        channel: str, oldest: str | None, latest: str | None
    ) -> Iterator[list[dict]]:
        for number, page in enumerate(served(channel, oldest, latest), start=1):
            if number == 2:
                raise KeyboardInterrupt
            yield page

    monkeypatch.setattr(h.gateway, "history", interrupted_on_page_2)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)

    counts = h.last_run().counts
    assert counts["threads_read"] == {"history": 1}
    api_calls = counts["api_calls"]
    assert isinstance(api_calls, dict)
    assert (api_calls["auth.test"], api_calls["conversations.replies"]) == (1, 1)


def test_interrupted_backfill_resumes_from_oldest_ts(h: Harness, monkeypatch: pytest.MonkeyPatch):
    stamps = [ts_on(18, hour) for hour in range(7)]
    h.serve(stamps, page_size=2)
    served = h.gateway.history

    def interrupted_on_page_3(
        channel: str, oldest: str | None, latest: str | None
    ) -> Iterator[list[dict]]:
        for number, page in enumerate(served(channel, oldest, latest), start=1):
            if number == 3:
                raise KeyboardInterrupt
            yield page

    monkeypatch.setattr(h.gateway, "history", interrupted_on_page_3)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)

    [partial] = h.last_run().tracked
    assert (partial.newest_ts, partial.oldest_ts) == (stamps[6], stamps[3])
    assert partial.backfill_complete is False
    assert message_ts_since(h, 0) == stamps[3:][::-1]

    monkeypatch.undo()
    h.gateway.calls.clear()
    h.pull()

    assert sorted(message_ts_since(h, 0)) == stamps
    assert history_calls(h) == [(None, stamps[3]), (None, stamps[3]), (stamps[6], None)]
    [item] = h.tracked()
    assert (item.newest_ts, item.oldest_ts, item.backfill_complete) == (stamps[6], stamps[0], True)


def interrupt_history(h: Harness, monkeypatch: pytest.MonkeyPatch, on_page: int) -> None:
    """Raises KeyboardInterrupt when history reaches the given page."""
    served = h.gateway.history

    def interrupted(channel: str, oldest: str | None, latest: str | None) -> Iterator[list[dict]]:
        for number, page in enumerate(served(channel, oldest, latest), start=1):
            if number == on_page:
                raise KeyboardInterrupt
            yield page

    monkeypatch.setattr(h.gateway, "history", interrupted)


def test_interrupted_first_pull_keeps_the_new_item(h: Harness, monkeypatch: pytest.MonkeyPatch):
    interrupt_history(h, monkeypatch, on_page=1)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)
    [item] = h.last_run().tracked
    assert (item.channel, item.backfill_complete, item.oldest_ts) == (CHANNEL, False, None)

    monkeypatch.undo()
    h.pull()
    [item] = h.tracked()
    assert item.backfill_complete is True


def test_interrupted_pull_keeps_a_widened_since(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.serve([ts_on(17, 9), ts_on(18, 9)])
    h.pull(BASE, options=PullOptions(since="2026-10-18"))
    interrupt_history(h, monkeypatch, on_page=1)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE, options=PullOptions(since="2026-10-17"))
    [item] = h.tracked()
    assert (item.since, item.backfill_complete) == (
        date_to_ts("2026-10-17", end_of_day=False),
        False,
    )


def test_interrupted_pull_keeps_every_new_item(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.gateway.conversations[SECOND_CHANNEL] = [message(ts_on(18, 9))]
    interrupt_history(h, monkeypatch, on_page=1)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE, SECOND_LINK, f"{BASE}/p1792410693663869")
    assert {(item.kind, item.channel) for item in h.tracked()} == {
        ("conversation", CHANNEL),
        ("conversation", SECOND_CHANNEL),
        ("thread", CHANNEL),
    }

    monkeypatch.undo()
    h.pull()
    assert {item.channel for item in h.tracked() if item.backfill_complete} == {
        CHANNEL,
        SECOND_CHANNEL,
    }
    assert SECOND_CHANNEL in {env.meta.channel for env in h.lines("message")}


def test_wider_since_backfills_gap(h: Harness):
    stamps = {day: ts_on(day, 12) for day in (17, 18, 19)}
    h.serve(list(stamps.values()))
    h.pull(BASE, options=PullOptions(since="2026-10-19"))
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    h.pull(BASE, options=PullOptions(since="2026-10-17"))

    assert message_ts_since(h, before) == [stamps[18], stamps[17]]
    assert history_calls(h) == [("1792195200.000000", stamps[19]), (stamps[19], None)]
    [item] = h.tracked()
    assert item.since == "1792195200.000000"
    assert (item.oldest_ts, item.backfill_complete) == (stamps[17], True)


def test_later_until_widens_range(h: Harness):
    stamps = {day: ts_on(day, 12) for day in (17, 18, 19)}
    h.serve(list(stamps.values()))
    h.pull(BASE, options=PullOptions(until="2026-10-18"))
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    h.pull(BASE, options=PullOptions(until="2026-10-19"))

    assert message_ts_since(h, before) == [stamps[19]]
    assert history_calls(h) == [(stamps[18], "1792454400.000000")]
    [item] = h.tracked()
    assert (item.until, item.newest_ts) == ("1792454400.000000", stamps[19])


def test_flush_called_per_page(h: Harness, monkeypatch: pytest.MonkeyPatch):
    h.serve([ts_on(18, hour) for hour in range(5)], page_size=2)
    served = h.gateway.history
    flushes_before_page: list[int] = []

    def recording(channel: str, oldest: str | None, latest: str | None) -> Iterator[list[dict]]:
        for page in served(channel, oldest, latest):
            flushes_before_page.append(h.archive.flushes)
            yield page

    monkeypatch.setattr(h.gateway, "history", recording)

    h.pull(BASE)

    counts = [*flushes_before_page, h.archive.flushes]
    assert len(flushes_before_page) == 3
    assert all(earlier < later for earlier, later in pairwise(counts))


def test_not_visible_conversation_is_skipped_with_warning(h: Harness):
    h.pull(BASE)
    before = len(h.archive.lines)
    del h.gateway.conversations[CHANNEL]

    report = h.pull()

    assert [env.meta.kind for env in h.archive.lines[before:]] == ["run"]
    [item] = h.tracked()
    assert item.status == "not_visible"
    assert h.reporter.warnings == [f"Conversation {CHANNEL} is not visible to this account."]
    assert report.skipped == h.reporter.warnings


def test_thread_missing_during_pull_is_skipped(h: Harness, monkeypatch: pytest.MonkeyPatch):
    def missing(channel: str, ts: str) -> list[dict]:
        raise ThreadNotFound(channel, ts)

    monkeypatch.setattr(h.gateway, "replies", missing)

    report = h.pull(BASE)

    assert len(h.lines("message")) == len(spike("conversations.history")["messages"])
    assert report.skipped == [f"Slack returns no message at {BASE}/p1792410693663869; skipping it."]
    [item] = h.tracked()
    assert (item.status, item.backfill_complete) == ("ok", True)


def test_failed_thread_read_warns_and_is_retried_next_pull(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    served = h.gateway.replies

    def unreachable(channel: str, ts: str) -> list[dict]:
        raise SlarcError(UNREACHABLE)

    monkeypatch.setattr(h.gateway, "replies", unreachable)
    report = h.pull(BASE)

    link = f"{BASE}/p1792410693663869"
    assert report.skipped == [
        f"Could not read the thread at {link}, so it is retried on the next pull: {UNREACHABLE}"
    ]
    assert report.items_pulled == 1
    [item] = h.tracked()
    assert (item.backfill_complete, item.retry_threads) == (True, [PARENT_TS])
    assert REPLY_TS not in {env.meta.ts for env in h.lines("message")}

    monkeypatch.setattr(h.gateway, "replies", served)
    h.pull()

    assert REPLY_TS in {env.meta.ts for env in h.lines("message")}
    threads_read = h.last_run().counts["threads_read"]
    assert isinstance(threads_read, dict)
    assert threads_read["retry"] == 1
    [item] = h.tracked()
    assert item.retry_threads == []


def test_failed_history_page_skips_the_conversation_and_pulls_the_rest(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    second_ts = ts_on(18, 9)
    h.gateway.conversations[SECOND_CHANNEL] = [message(second_ts)]
    h.serve([ts_on(18, hour) for hour in range(3)], page_size=2)
    served = h.gateway.history
    error = "Could not reach Slack to call conversations.history."

    def failing_on_page_2(
        channel: str, oldest: str | None, latest: str | None
    ) -> Iterator[list[dict]]:
        for number, page in enumerate(served(channel, oldest, latest), start=1):
            if channel == CHANNEL and number == 2:
                raise SlarcError(error)
            yield page

    monkeypatch.setattr(h.gateway, "history", failing_on_page_2)
    report = h.pull(BASE, SECOND_LINK)

    assert report.items_pulled == 1
    assert report.skipped == [
        f"Could not finish pulling conversation {CHANNEL}; the next pull resumes it: {error}"
    ]
    assert second_ts in {
        env.meta.ts for env in h.lines("message") if env.meta.channel == SECOND_CHANNEL
    }
    tracked = {item.channel: item for item in h.tracked()}
    assert tracked[CHANNEL].backfill_complete is False
    assert tracked[SECOND_CHANNEL].backfill_complete is True


def test_auth_failure_on_a_thread_read_still_skips_the_workspace(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    def rejected(channel: str, ts: str) -> list[dict]:
        raise AuthFailed("desktop", "naleidworkspace")

    monkeypatch.setattr(h.gateway, "replies", rejected)
    report = h.pull(BASE)

    assert report.failed is True
    assert h.last_run().errors[0]["team_id"] == TEAM


def test_archive_write_failure_aborts_the_pull(h: Harness, monkeypatch: pytest.MonkeyPatch):
    appended = h.archive.append

    def failing_on_messages(env: Envelope) -> None:
        if env.meta.kind == "message":
            raise ArchiveWriteFailed(Path("raw/2026-10.jsonl"), "No space left on device")
        appended(env)

    monkeypatch.setattr(h.archive, "append", failing_on_messages)

    with pytest.raises(ArchiveWriteFailed):
        h.pull(BASE)


def test_retry_thread_turning_not_visible_stays_in_retry_threads(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    def unreachable(channel: str, ts: str) -> list[dict]:
        raise SlarcError(UNREACHABLE)

    monkeypatch.setattr(h.gateway, "replies", unreachable)
    h.pull(BASE)
    [item] = h.tracked()
    assert item.retry_threads == [PARENT_TS]

    def not_visible(channel: str, ts: str) -> list[dict]:
        raise NotVisible(channel)

    monkeypatch.setattr(h.gateway, "replies", not_visible)
    h.pull()

    [item] = h.tracked()
    assert (item.status, item.retry_threads) == ("not_visible", [PARENT_TS])


def test_workspace_selector_refreshes_what_is_tracked_there(h: Harness):
    h.serve([ts_on(18, 9)])
    h.pull(BASE)
    newer = ts_on(19, 9)
    h.gateway.conversations[CHANNEL].append(message(newer))
    before = len(h.archive.lines)

    report = h.pull("naleidworkspace")

    assert report.items_selected == 1
    assert message_ts_since(h, before) == [newer]


def test_workspace_selector_leaves_other_workspaces_alone(h: Harness):
    other = other_gateway()
    h.connector.gateways = {NALEID: h.gateway, OTHER: other}
    h.serve([ts_on(18, 9)])
    h.pull(BASE, OTHER_LINK)
    h.gateway.conversations[CHANNEL].append(message(ts_on(19, 9)))
    other.conversations[OTHER_CHANNEL].append(message(ts_on(19, 10)))
    before = len(h.archive.lines)

    h.pull("other")

    assert message_ts_since(h, before) == [ts_on(19, 10)]
    assert h.connector.connects.count("naleidworkspace") == 1


def test_workspace_selector_with_nothing_tracked_there_fails(h: Harness):
    with pytest.raises(SlarcError) as caught:
        h.pull("naleidworkspace")
    assert str(caught.value) == (
        "Nothing is tracked in workspace naleidworkspace. "
        "Pull a Slack link from it to start tracking it."
    )


def test_workspace_selector_keeps_an_item_an_earlier_selector_widened(h: Harness):
    h.pull(BASE, options=PullOptions(since="2026-10-18"))
    [narrow] = h.tracked()

    h.pull(BASE, "naleidworkspace", options=PullOptions(since="2026-10-01"))

    [widened] = h.tracked()
    assert narrow.since is not None
    assert widened.since == date_to_ts("2026-10-01", end_of_day=False)


def test_interrupted_thread_read_keeps_lines_appended_before_it(
    h: Harness, monkeypatch: pytest.MonkeyPatch
):
    first, second = ts_on(18, 1), ts_on(18, 3)
    stamps = [first, ts_on(18, 2), second, ts_on(18, 4)]
    h.gateway.conversations[CHANNEL] = [
        message(first, thread_ts=first, reply_count=1),
        message(stamps[1], thread_ts=first),
        message(second, thread_ts=second, reply_count=1),
        message(stamps[3], thread_ts=second),
    ]
    served = h.gateway.replies

    def interrupted_on_first(channel: str, ts: str) -> list[dict]:
        if ts == first:
            raise KeyboardInterrupt
        return served(channel, ts)

    monkeypatch.setattr(h.gateway, "replies", interrupted_on_first)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)

    assert sorted(message_ts_since(h, 0)) == [first, second, stamps[3]]
    [item] = h.last_run().tracked
    assert (item.oldest_ts, item.backfill_complete) == (None, False)

    monkeypatch.undo()
    h.pull()

    assert sorted(message_ts_since(h, 0)) == stamps
