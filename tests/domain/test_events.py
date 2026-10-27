# ABOUTME: Tests for classifying RTM events, over events recorded from the test workspace, and for
# ABOUTME: which tracked item covers a message.
import json
from pathlib import Path

import pytest

from slarc.domain.events import (
    ENVELOPE_KEYS,
    DeleteMessage,
    EmojiChanged,
    Reread,
    StoreMessage,
    UserChanged,
    classify,
)
from slarc.domain.hashing import content_hash
from slarc.domain.tracking import TrackedItem, covers

RTM = Path(__file__).parent.parent / "fixtures" / "rtm"
EVENTS = [json.loads(line) for line in (RTM / "events.jsonl").read_text().splitlines()]
[REPLIES_PARENT] = json.loads((RTM / "replies.json").read_text())["messages"]
CHANNEL = "C0CAL2LPBHA"
PARENT = "1793134571.213779"
REPLY = "1793134571.481079"
TEAM = "T0CB5LYPVFA"


def events(type_: str, subtype: str | None = None) -> list[dict]:
    return [e for e in EVENTS if e.get("type") == type_ and e.get("subtype") == subtype]


def test_new_messages_are_stored_without_their_envelope_keys() -> None:
    stored = [classify(event) for event in events("message")]
    assert all(isinstance(change, StoreMessage) for change in stored)
    stored = [change for change in stored if isinstance(change, StoreMessage)]

    assert [(c.channel, c.message["ts"]) for c in stored] == [(CHANNEL, PARENT), (CHANNEL, REPLY)]
    for change in stored:
        assert not set(ENVELOPE_KEYS) & change.message.keys()
        assert {"text", "blocks", "user", "ts"} <= change.message.keys()
    assert stored[1].message["thread_ts"] == PARENT


def test_edits_store_the_message_as_conversations_replies_returns_it() -> None:
    edits = [classify(event) for event in events("message", "message_changed")]

    assert len(edits) == 2
    for change in edits:
        assert isinstance(change, StoreMessage)
        assert change.channel == CHANNEL
        assert content_hash("message", change.message) == content_hash("message", REPLIES_PARENT)


def test_a_delete_names_the_deleted_message() -> None:
    [event] = events("message", "message_deleted")

    assert classify(event) == DeleteMessage(CHANNEL, REPLY)


def test_reactions_reread_the_message_they_are_on() -> None:
    for type_ in ("reaction_added", "reaction_removed"):
        [event] = events(type_)
        assert classify(event) == Reread(CHANNEL, PARENT)


@pytest.mark.parametrize(
    "type_",
    ["hello", "channel_marked", "thread_subscribed", "badge_counts_updated", "activity"],
)
def test_events_about_read_state_or_notifications_change_nothing(type_: str) -> None:
    matching = [e for e in EVENTS if e.get("type") == type_]
    assert matching
    assert {classify(event) for event in matching} == {None}


def test_a_parent_update_for_a_new_reply_changes_nothing() -> None:
    [event] = events("message", "message_replied")

    assert classify(event) is None


def test_user_and_emoji_changes() -> None:
    user = {"id": "U0NEWUSER1", "name": "ada"}

    assert classify({"type": "user_change", "user": user}) == UserChanged(user)
    assert classify({"type": "emoji_changed", "subtype": "add", "name": "x"}) == EmojiChanged()
    assert classify({"type": "pong", "reply_to": 1}) is None


def conversation(since: str | None = None, until: str | None = None) -> TrackedItem:
    return TrackedItem(team_id=TEAM, kind="conversation", channel=CHANNEL, since=since, until=until)


@pytest.mark.parametrize(
    ("item", "channel", "ts", "thread_ts", "covered"),
    [
        (conversation(), CHANNEL, PARENT, None, True),
        (conversation(), "C0OTHER0001", PARENT, None, False),
        (conversation(until=PARENT), CHANNEL, PARENT, None, False),
        (conversation(until=REPLY), CHANNEL, PARENT, None, True),
        (conversation(since=PARENT), CHANNEL, PARENT, None, False),
        (conversation(since="1793134000.000000"), CHANNEL, REPLY, PARENT, True),
        (
            TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT),
            CHANNEL,
            REPLY,
            PARENT,
            True,
        ),
        (
            TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT),
            CHANNEL,
            PARENT,
            None,
            True,
        ),
        (
            TrackedItem(team_id=TEAM, kind="thread", channel=CHANNEL, ts=PARENT),
            CHANNEL,
            "1.000100",
            None,
            False,
        ),
        (
            TrackedItem(team_id=TEAM, kind="message", channel=CHANNEL, ts=PARENT),
            CHANNEL,
            PARENT,
            None,
            True,
        ),
        (
            TrackedItem(team_id=TEAM, kind="message", channel=CHANNEL, ts=PARENT),
            CHANNEL,
            REPLY,
            PARENT,
            False,
        ),
    ],
)
def test_covers(
    item: TrackedItem, channel: str, ts: str, thread_ts: str | None, covered: bool
) -> None:
    assert covers(item, channel, ts, thread_ts) is covered
