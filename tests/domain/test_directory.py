# ABOUTME: Tests for DirectoryView name lookups over users, conversations, usergroups and authors.
# ABOUTME: Uses the spike users.list fixture, whose human users have empty display names.
import json
from pathlib import Path

from slarc.domain.directory import DirectoryView

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"


def _view() -> DirectoryView:
    members = json.loads((SPIKE / "users.list.json").read_text())["members"]
    return DirectoryView(
        users={m["id"]: m for m in members},
        conversations={
            "C1": {"id": "C1", "name": "general"},
            "D1": {"id": "D1", "is_im": True, "user": "U0CA61XJF2R"},
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


def test_author_display_prefers_user_then_bot_profile_then_username():
    view = _view()
    assert (
        view.author_display({"user": "U0CA538UEN7", "bot_profile": {"name": "giphy"}})
        == "Ted Naleid"
    )
    assert view.author_display({"bot_profile": {"name": "giphy"}, "username": "x"}) == "giphy"
    assert view.author_display({"username": "webhook"}) == "webhook"
