# ABOUTME: Tests for which stored messages tracked items cover once retention applies.
# ABOUTME: A thread is kept whole while its newest message is newer than the item's cutoff.
from datetime import UTC, datetime, timedelta

from slarc.domain.envelope import Envelope, Meta
from slarc.domain.tracking import TrackedItem, WorkspaceSetting
from slarc.service.selectors import covered_stamps

TEAM = "T0CB5LYPVFA"
CHANNEL = "C0CA5396G03"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def days_ago(days: int) -> str:
    return f"{(NOW - timedelta(days=days)).timestamp():.6f}"


OLD_LONE = days_ago(40)
OLD_PARENT = days_ago(41)
RECENT_REPLY = days_ago(2)
EXPIRED_PARENT = days_ago(50)
EXPIRED_REPLY = days_ago(45)
RECENT_LONE = days_ago(1)


def line(ts: str, thread_ts: str | None = None) -> list[Envelope]:
    message = {"ts": ts} | ({"thread_ts": thread_ts} if thread_ts else {})
    return [Envelope(meta=Meta(kind="message", team=TEAM, channel=CHANNEL, ts=ts), object=message)]


VERSIONS = {
    OLD_LONE: line(OLD_LONE),
    OLD_PARENT: line(OLD_PARENT, OLD_PARENT),
    RECENT_REPLY: line(RECENT_REPLY, OLD_PARENT),
    EXPIRED_PARENT: line(EXPIRED_PARENT, EXPIRED_PARENT),
    EXPIRED_REPLY: line(EXPIRED_REPLY, EXPIRED_PARENT),
    RECENT_LONE: line(RECENT_LONE),
}
CONVERSATION = TrackedItem(team_id=TEAM, kind="conversation", channel=CHANNEL)
MONTH = {TEAM: WorkspaceSetting(team_id=TEAM, retention=30)}


def test_without_retention_a_conversation_covers_every_stored_message() -> None:
    assert covered_stamps(VERSIONS, [CONVERSATION], {}, NOW) == set(VERSIONS)


def test_retention_keeps_threads_with_recent_activity_and_drops_the_rest() -> None:
    assert covered_stamps(VERSIONS, [CONVERSATION], MONTH, NOW) == {
        OLD_PARENT,
        RECENT_REPLY,
        RECENT_LONE,
    }


def test_an_items_own_retention_overrides_its_workspaces() -> None:
    own = TrackedItem(team_id=TEAM, kind="conversation", channel=CHANNEL, retention=60)
    assert covered_stamps(VERSIONS, [own], MONTH, NOW) == set(VERSIONS)


def test_a_forever_thread_inside_a_conversation_with_retention_keeps_that_thread() -> None:
    thread = TrackedItem(
        team_id=TEAM, kind="thread", channel=CHANNEL, ts=EXPIRED_PARENT, retention="forever"
    )
    assert covered_stamps(VERSIONS, [CONVERSATION, thread], MONTH, NOW) == {
        OLD_PARENT,
        RECENT_REPLY,
        RECENT_LONE,
        EXPIRED_PARENT,
        EXPIRED_REPLY,
    }


def test_a_message_item_past_its_retention_covers_nothing() -> None:
    lone = TrackedItem(team_id=TEAM, kind="message", channel=CHANNEL, ts=OLD_LONE)
    assert covered_stamps(VERSIONS, [lone], MONTH, NOW) == set()
