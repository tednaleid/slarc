# ABOUTME: Tests for pulling whole conversations: date ranges, paging, backfill and thread reads.
# ABOUTME: A fake gateway pages history newest first; some tests interrupt or observe its paging.
from collections.abc import Iterator
from itertools import pairwise

import pytest

from slarc.ports import AuthFailed, SlarcError, ThreadNotFound
from slarc.service.pull import PullOptions, date_to_ts
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    PARENT_TS,
    REPLY_TS,
    TEAM,
    THREAD_TS,
    Harness,
    channel_messages,
    message,
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


def test_progress_reported_per_page_and_item(h: Harness):
    stamps = [ts_on(18, hour) for hour in range(7)]
    h.serve(stamps, page_size=3)

    report = h.pull(BASE)

    assert h.reporter.progress_updates == [
        ("#all-naleid", 0, None),
        ("#all-naleid", 3, None),
        ("#all-naleid", 6, None),
        ("#all-naleid", 7, None),
        ("#all-naleid", 7, 7),
    ]
    assert report.items_pulled == 1


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
