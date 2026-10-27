# ABOUTME: Tests for DirectoryView name lookups over users, conversations, usergroups and authors.
# ABOUTME: Uses the spike users.list fixture, whose human users have empty display names.
import json
from pathlib import Path

from slarc.domain.directory import DirectoryView, conversation_kind, referenced_users

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"


def _view() -> DirectoryView:
    members = json.loads((SPIKE / "users.list.json").read_text())["members"]
    return DirectoryView(
        users={m["id"]: m for m in members},
        conversations={
            "C1": {"id": "C1", "name": "general"},
            "D1": {"id": "D1", "is_im": True, "user": "U0CA61XJF2R"},
            "G1": {"id": "G1", "name": "mpdm-a--b-1", "is_mpim": True},
        },
        usergroups={"S1": {"id": "S1", "handle": "eng"}},
        emoji={},
        bots={},
    )


def test_display_name_fallback_to_real_name():
    view = _view()
    assert view.user_display("U0CA538UEN7") == "Ted Naleid"
    assert view.user_display("U0CA61XJF2R") == "jim"


def test_display_name_prefers_profile_display_name():
    view = DirectoryView(
        users={"U1": {"name": "n", "real_name": "R", "profile": {"display_name": "D"}}},
        conversations={},
        usergroups={},
        emoji={},
        bots={},
    )
    assert view.user_display("U1") == "D"


def test_unknown_user_id_renders_raw_id():
    assert _view().user_display("UNOBODY") == "UNOBODY"


def test_conversation_name_and_usergroup_handle():
    view = _view()
    assert view.conversation_name("C1") == "general"
    assert view.conversation_name("D1") == "jim"
    assert view.conversation_name("CUNKNOWN") == "CUNKNOWN"
    assert view.usergroup_handle("S1") == "eng"
    assert view.usergroup_handle("SX") == "SX"


def test_conversation_label_and_kind():
    view = _view()
    assert view.conversation_label("C1") == "#general"
    assert view.conversation_label("D1") == "@jim"
    assert view.conversation_label("G1") == "mpdm-a--b-1"
    assert view.conversation_label("CUNKNOWN") == "CUNKNOWN"
    assert [conversation_kind(view.conversations[c]) for c in ("C1", "D1", "G1")] == [
        "channel",
        "dm",
        "group_dm",
    ]


def test_author_display_prefers_user_then_bot_profile_then_username():
    view = _view()
    assert (
        view.author_display({"user": "U0CA538UEN7", "bot_profile": {"name": "giphy"}})
        == "Ted Naleid"
    )
    assert view.author_display({"bot_profile": {"name": "giphy"}, "username": "x"}) == "giphy"
    assert view.author_display({"username": "webhook"}) == "webhook"


def test_referenced_users_finds_every_user_id_a_message_names():
    message = {
        "user": "U0AUTHOR1",
        "parent_user_id": "U0PARENT1",
        "text": "hi <@U0MRKDWN1> and <@W0GRID001|someone>",
        "reply_users": ["U0REPLY01"],
        "reactions": [{"name": "eyes", "users": ["U0REACT01"], "count": 1}],
        "blocks": [
            {
                "type": "rich_text",
                "elements": [{"elements": [{"type": "user", "user_id": "U0RICH001"}]}],
            }
        ],
        "attachments": [{"is_msg_unfurl": True, "author_id": "U0UNFURL1", "text": "<@U0NESTED1>"}],
        "edited": {"user": "U0EDITOR1", "ts": "1.0"},
        "bot_id": "B0BOT0001",
        "channel": "C0CHANNEL",
    }

    assert referenced_users(message) == {
        "U0AUTHOR1",
        "U0PARENT1",
        "U0MRKDWN1",
        "W0GRID001",
        "U0REPLY01",
        "U0REACT01",
        "U0RICH001",
        "U0UNFURL1",
        "U0NESTED1",
        "U0EDITOR1",
    }


def test_referenced_users_ignores_values_that_are_not_user_ids():
    assert referenced_users({"user": "", "text": "<@here> <#C0CHANNEL>", "users": [3]}) == set()
