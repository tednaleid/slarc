# ABOUTME: Tests for pulling whole conversations: date ranges, paging, backfill and thread reads.
# ABOUTME: A fake gateway pages history newest first; some tests interrupt or observe its paging.
from collections.abc import Iterator
from itertools import pairwise

import pytest

from slarc.ports import ThreadNotFound
from slarc.service.pull import PullOptions, date_to_ts
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    ME,
    PARENT_TS,
    THREAD_TS,
    Harness,
    channel_messages,
    spike,
)

OCT_17 = 1792195200
BROADCAST_TS = "1792422700.000100"


def ts_on(day: int, hour: int) -> str:
    """A ts at the given hour of the given day of October 2026, UTC."""
    return f"{OCT_17 + (day - 17) * 86400 + hour * 3600}.000100"


def message(ts: str, **extra: object) -> dict:
    return {"type": "message", "user": ME, "text": f"message at {ts}", "ts": ts, **extra}


def serve(h: Harness, stamps: list[str], page_size: int = 200) -> None:
    h.gateway.conversations[CHANNEL] = [message(ts) for ts in stamps]
    h.gateway.page_size = page_size


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
    serve(h, stamps, page_size=3)

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


def test_since_until_bound_history(h: Harness):
    stamps = {day: ts_on(day, 12) for day in (17, 18, 19, 20)}
    serve(h, list(stamps.values()))

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
    serve(h, stamps)
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


def test_interrupted_backfill_resumes_from_oldest_ts(h: Harness, monkeypatch: pytest.MonkeyPatch):
    stamps = [ts_on(18, hour) for hour in range(7)]
    serve(h, stamps, page_size=2)
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
    serve(h, list(stamps.values()))
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
    serve(h, list(stamps.values()))
    h.pull(BASE, options=PullOptions(until="2026-10-18"))
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    h.pull(BASE, options=PullOptions(until="2026-10-19"))

    assert message_ts_since(h, before) == [stamps[19]]
    assert history_calls(h) == [(stamps[18], "1792454400.000000")]
    [item] = h.tracked()
    assert (item.until, item.newest_ts) == ("1792454400.000000", stamps[19])


def test_flush_called_per_page(h: Harness, monkeypatch: pytest.MonkeyPatch):
    serve(h, [ts_on(18, hour) for hour in range(5)], page_size=2)
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
