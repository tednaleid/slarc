# ABOUTME: Tests for plain_text, which renders a message as readable searchable text.
# ABOUTME: Uses spike messages for rich text, giphy blocks, link previews and files.
import json
from pathlib import Path

from slarc.domain.directory import DirectoryView
from slarc.domain.plain import plain_text, snippet

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"


def _load(name: str) -> dict:
    return json.loads((SPIKE / name).read_text())


def _view() -> DirectoryView:
    members = _load("users.list.json")["members"]
    return DirectoryView(
        users={m["id"]: m for m in members},
        conversations={"C0CA5396G03": {"id": "C0CA5396G03", "name": "general"}},
        usergroups={"S1": {"id": "S1", "handle": "eng"}},
        emoji={},
        bots={},
    )


def _replies() -> list[dict]:
    return _load("conversations.replies.json")["messages"]


def test_plain_rich_text_with_emoji():
    message = _replies()[1]
    assert plain_text(message, _view()) == "Ted Naleid: Hey Jim! It's good to see you here! :smile:"


def test_plain_giphy_uses_alt_text_and_context():
    message = _replies()[2]
    assert plain_text(message, _view()) == (
        "Ted Naleid: some men just want to watch the world burn Posted using /giphy"
    )


def test_plain_link_preview_includes_title():
    message = _replies()[3]
    assert plain_text(message, _view()) == (
        "Ted Naleid: a screenshot attachment and a link with preview https://github.com/tednaleid"
        " tednaleid - Overview CleanShot 2026-10-19 at 10.09.44.png"
    )


def test_plain_snippet_message_includes_file_name():
    message = _load("conversations.history.json")["messages"][0]
    plain = plain_text(message, _view())
    assert plain.startswith("Ted Naleid: yet another snippet that should be 10k lines long")
    assert plain.endswith("10k-snippet.txt")


def test_plain_mrkdwn_fallback_resolves_mentions_and_unescapes():
    message = {
        "user": "U0CA61XJF2R",
        "text": "hi <@U0CA538UEN7> see <#C0CA5396G03|general> "
        "<https://x.test/a?b=1&amp;c=2|the doc> <https://y.test> "
        "a &lt; b &amp;&amp; c &gt; d <!subteam^S1|@eng> <!here>",
    }
    assert plain_text(message, _view()) == (
        "jim: hi @Ted Naleid see #general the doc https://y.test a < b && c > d @eng @here"
    )


def test_plain_rich_text_mentions_channels_links_and_unknown_elements():
    message = {
        "user": "U0CA61XJF2R",
        "text": "ignored",
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "user", "user_id": "U0CA538UEN7"},
                            {"type": "text", "text": " in "},
                            {"type": "channel", "channel_id": "C0CA5396G03"},
                            {"type": "text", "text": " "},
                            {"type": "link", "url": "https://a.test", "text": "label"},
                            {"type": "text", "text": " "},
                            {"type": "usergroup", "usergroup_id": "S1"},
                            {"type": "text", "text": " "},
                            {"type": "broadcast", "range": "here"},
                            {"type": "mystery", "foo": 1},
                        ],
                    },
                    {"type": "mystery_block"},
                ],
            },
            {"type": "mystery_layout"},
        ],
    }
    assert plain_text(message, _view()) == "jim: @Ted Naleid in #general label @eng @here"


def test_plain_layout_blocks_and_skin_tone_emoji():
    message = {
        "bot_profile": {"name": "bot"},
        "text": "fallback",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Title"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Body &amp; more"}},
            {"type": "divider"},
        ],
    }
    assert plain_text(message, _view()) == "bot: fallback Title Body & more"
    rich = {
        "user": "U0CA61XJF2R",
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "emoji", "name": "wave::skin-tone-3"}],
                    }
                ],
            }
        ],
    }
    assert plain_text(rich, _view()) == "jim: :wave::skin-tone-3:"


def test_snippet_collapses_whitespace_of_short_text():
    assert snippet("Ted Naleid: one\n\ntwo  three", "two") == "Ted Naleid: one two three"


def test_snippet_of_long_text_centers_on_the_match():
    plain = " ".join(["before"] * 40) + " the NEEDLE here " + " ".join(["after"] * 40)
    shown = snippet(plain, "needle", width=60)
    assert "the NEEDLE here" in shown
    assert shown.startswith("...") and shown.endswith("...")
    assert len(shown) <= 66


def test_snippet_without_a_match_shows_the_start():
    plain = "start " + "x" * 200
    assert snippet(plain, "absent", width=20) == "start xxxxxxxxxxxxxx..."
