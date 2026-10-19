# ABOUTME: Tests for finding replies under old parents, re-reading active threads and --full pulls.
# ABOUTME: Covers delete inference, which never reaches below what Slack still serves.
from datetime import UTC, datetime

import pytest

from slarc.domain.links import MessageTs
from slarc.ports import SlarcError, StoredMessage
from slarc.service.pull import PullOptions
from slarc.service.threads import active_threads, discover_reply_threads, infer_deletes
from tests.fakes import FakeSlackGateway
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    PARENT_TS,
    Harness,
    message,
    spike,
    ts_on,
)

OTHER = "C0OTHER0001"
FIRST_SEARCH = f"in:<#{CHANNEL}> after:2026-10-18"
NOW = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)


def search_match(ts: str, thread_ts: str | None = None) -> dict:
    """A search.messages match as the spike recorded it: thread_ts only in the permalink."""
    permalink = f"{BASE}/p{ts.replace('.', '')}"
    if thread_ts is not None:
        permalink += f"?thread_ts={thread_ts}"
    return {"type": "message", "ts": ts, "channel": {"id": CHANNEL}, "permalink": permalink}


def thread(parent: str, *replies: str) -> list[dict]:
    return [
        message(parent, thread_ts=parent, reply_count=len(replies)),
        *(message(ts, thread_ts=parent) for ts in replies),
    ]


def replies_calls(h: Harness) -> list[str | None]:
    return [call[2] for call in h.gateway.calls if call[0] == "conversations.replies"]


def search_calls(h: Harness) -> list[str | None]:
    return [call[1] for call in h.gateway.calls if call[0] == "search.messages"]


def lines_since(h: Harness, before: int) -> list[tuple[str, str | None]]:
    return [(env.meta.kind, env.meta.ts) for env in h.archive.lines[before:]]


def deleted_ts(h: Harness) -> set[str | None]:
    return {env.meta.ts for env in h.lines("deleted")}


def stored(thread_ts: str | None = None, deleted: bool = False) -> StoredMessage:
    return StoredMessage(None if deleted else "hash", thread_ts, "2026-10-19T13:40:11Z")


@pytest.fixture
def h() -> Harness:
    return Harness()


def test_new_reply_under_old_parent_found_by_search(h: Harness):
    parent, old_reply, newest = ts_on(1, 9), ts_on(1, 10), ts_on(18, 9)
    h.gateway.conversations[CHANNEL] = [*thread(parent, old_reply), message(newest)]
    h.pull(BASE)
    [item] = h.tracked()
    assert item.newest_ts is not None and MessageTs(parent) < item.newest_ts
    new_reply = ts_on(19, 14)
    h.gateway.conversations[CHANNEL].append(message(new_reply, thread_ts=parent))
    h.change_message(parent, reply_count=2)
    h.gateway.search_results[FIRST_SEARCH] = [search_match(new_reply, parent)]
    before = len(h.archive.lines)

    h.pull()

    assert lines_since(h, before) == [("message", new_reply), ("run", None)]
    assert h.archive.lines[before].meta.api == "conversations.replies"
    [item] = h.tracked()
    assert item.last_search_at == "2026-10-19T14:40:11Z"


def test_search_query_uses_last_search_date_minus_one_day(h: Harness):
    h.serve([ts_on(18, 9)])
    h.pull(BASE)
    [item] = h.tracked()
    assert item.last_search_at == "2026-10-19T13:40:11Z"
    assert search_calls(h) == []
    h.clock.advance(2 * 86400)

    h.pull()
    h.pull()

    assert search_calls(h) == [FIRST_SEARCH, f"in:<#{CHANNEL}> after:2026-10-20"]


def test_active_threads_reread(h: Harness):
    parent, reply = ts_on(16, 9), ts_on(17, 9)
    h.gateway.conversations[CHANNEL] = thread(parent, reply)
    h.pull(BASE)
    h.change_message(reply, text="edited reply", edited={"user": "U1", "ts": ts_on(19, 14)})
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    h.pull()

    assert replies_calls(h) == [parent]
    assert lines_since(h, before) == [("message", reply), ("run", None)]


def test_dormant_thread_not_reread_without_search_hit(h: Harness):
    parent, reply = ts_on(1, 9), ts_on(2, 9)
    h.gateway.conversations[CHANNEL] = thread(parent, reply)
    h.pull(BASE)
    h.change_message(reply, text="edited reply", edited={"user": "U1", "ts": ts_on(19, 14)})
    h.gateway.calls.clear()
    before = len(h.archive.lines)

    h.pull()

    assert replies_calls(h) == []
    assert lines_since(h, before) == [("run", None)]


def test_full_pull_records_deletes_in_range(h: Harness):
    parent, reply, kept_reply = ts_on(1, 9), ts_on(1, 10), ts_on(1, 11)
    emptied, emptied_reply = ts_on(2, 9), ts_on(2, 10)
    top = [ts_on(3, hour) for hour in range(4)]
    h.gateway.conversations[CHANNEL] = [
        *thread(parent, reply, kept_reply),
        *thread(emptied, emptied_reply),
        *(message(ts) for ts in top),
    ]
    h.pull(BASE)
    h.remove_messages(top[1], reply, emptied_reply)
    h.change_message(emptied, reply_count=0)
    before = len(h.archive.lines)

    h.pull(options=PullOptions(full=True))

    assert deleted_ts(h) == {top[1], reply, emptied_reply}
    assert [kind for kind, _ in lines_since(h, before)] == ["deleted"] * 3 + ["run"]


def test_full_pull_does_not_delete_below_oldest_returned(h: Harness):
    stamps = [ts_on(5, hour) for hour in range(6)]
    h.serve(stamps)
    h.pull(BASE)
    h.remove_messages(stamps[0], stamps[1], stamps[3])

    h.pull(options=PullOptions(full=True))

    assert deleted_ts(h) == {stamps[3]}


def test_not_visible_conversation_is_skipped_without_deletes(h: Harness):
    h.serve([ts_on(18, hour) for hour in range(3)])
    h.gateway.conversations[OTHER] = [message(ts_on(18, 9))]
    h.pull(BASE, f"https://naleidworkspace.slack.com/archives/{OTHER}")
    del h.gateway.conversations[CHANNEL]
    newer = ts_on(19, 9)
    h.gateway.conversations[OTHER].append(message(newer))
    before = len(h.archive.lines)

    report = h.pull(options=PullOptions(full=True))

    assert h.lines("deleted") == []
    assert lines_since(h, before) == [("message", newer), ("run", None)]
    status = {item.channel: item.status for item in h.tracked()}
    assert status == {CHANNEL: "not_visible", OTHER: "ok"}
    assert report.skipped == [f"Conversation {CHANNEL} is not visible to this account."]


def test_search_failure_falls_back_to_active_threads(h: Harness, monkeypatch: pytest.MonkeyPatch):
    parent, reply = ts_on(16, 9), ts_on(17, 9)
    h.gateway.conversations[CHANNEL] = thread(parent, reply)
    h.pull(BASE)
    new_reply = ts_on(19, 14)
    h.gateway.conversations[CHANNEL].append(message(new_reply, thread_ts=parent))

    def failing(query: str) -> list[dict]:
        raise SlarcError("Slack returned invalid_query for search.messages.")

    monkeypatch.setattr(h.gateway, "search", failing)
    before = len(h.archive.lines)

    h.pull()

    assert lines_since(h, before) == [("message", new_reply), ("run", None)]
    assert h.reporter.warnings == [
        f"Could not search conversation {CHANNEL} for new replies, so only its active threads "
        "were re-read: Slack returned invalid_query for search.messages."
    ]
    [item] = h.tracked()
    assert item.last_search_at == "2026-10-19T13:40:11Z"


def test_discover_reply_threads_reads_thread_ts_from_permalinks():
    query = f"in:<#{CHANNEL}> after:2026-10-17"
    gateway = FakeSlackGateway(
        search_results={query: spike("search.messages.0")["messages"]["matches"]}
    )

    assert discover_reply_threads(gateway, CHANNEL, "2026-10-17") == {PARENT_TS}


def test_active_threads_have_a_reply_inside_the_window():
    recent_parent, old_parent = ts_on(1, 9), ts_on(2, 9)
    stored_messages = {
        recent_parent: stored(recent_parent),
        ts_on(13, 9): stored(recent_parent),
        old_parent: stored(old_parent),
        ts_on(11, 9): stored(old_parent),
        ts_on(18, 9): stored(),
        ts_on(18, 10): stored(ts_on(18, 10)),
    }

    assert active_threads(stored_messages, NOW, days=7) == {recent_parent}


def test_infer_deletes_counts_only_from_oldest_returned():
    stamps = [ts_on(5, hour) for hour in range(6)]
    in_range = {ts: stored() for ts in stamps} | {stamps[5]: stored(deleted=True)}
    returned = {stamps[2], stamps[4]}

    assert infer_deletes(in_range, returned, ts_on(4, 0), stamps[2]) == {stamps[3]}
    assert infer_deletes(in_range, returned, stamps[3], stamps[2]) == set()
    assert infer_deletes(in_range, set(), None, None) == set()
