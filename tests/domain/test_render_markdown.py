# ABOUTME: Tests for render_message_markdown: rich_text and layout blocks, mrkdwn, subtypes.
# ABOUTME: Mirrors the HTML renderer's cases, with mentions resolved and emoji as :name:.
from slarc.domain.render.markdown import render_message_markdown
from tests.recorded_fixtures import RECORDED_TEAM, recorded_lines, recorded_view

SEED_RICH_TEXT = "1792437053.095289"
GIPHY = "1792411464.844209"


def _md(message: dict) -> str:
    return render_message_markdown(message, recorded_view())


def _recorded(ts: str) -> dict:
    lines = recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    return next(line["object"] for line in lines if line["meta"]["ts"] == ts)


def _section(*elements: dict) -> dict:
    return {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_section", "elements": list(elements)}],
            }
        ]
    }


def test_rich_text_as_markdown():
    assert _md(_recorded(SEED_RICH_TEXT)) == (
        "seed: rich text with **bold**, _italic_, ~~struck out~~ and `inline code`. "
        "A link: [the docs](https://example.com/docs). Mentions: @Jim #slarc-fixtures @here\n"
        "- first bullet\n"
        "- second bullet\n"
        "  - nested bullet\n"
        "- third bullet\n"
        "1. step one\n"
        "2. step two\n"
        "   1. nested step\n"
        "> a quote\n"
        "```\n"
        "def hello():\n"
        "    return 'world'\n"
        "```"
    )


def test_list_offset_and_combined_styles():
    listed = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "style": "ordered",
                        "offset": 2,
                        "elements": [
                            {
                                "type": "rich_text_section",
                                "elements": [{"type": "text", "text": "c"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    assert _md(listed) == "3. c"
    styled = _section({"type": "text", "text": "x", "style": {"bold": True, "italic": True}})
    assert _md(styled) == "_**x**_"


def test_links_mentions_emoji_and_unsafe_links():
    message = _section(
        {"type": "link", "url": "https://example.com"},
        {"type": "text", "text": " "},
        {"type": "link", "url": "javascript:alert(1)", "text": "click"},
        {"type": "text", "text": " "},
        {"type": "user", "user_id": "UNKNOWN"},
        {"type": "emoji", "name": "wave", "skin_tone": 3},
        {"type": "date", "timestamp": 1792437053, "fallback": "Oct 20"},
    )
    assert _md(message) == "https://example.com click @UNKNOWN:wave::skin-tone-3:Oct 20"


def test_quote_spanning_lines():
    message = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {"type": "rich_text_quote", "elements": [{"type": "text", "text": "a\nb"}]}
                ],
            }
        ]
    }
    assert _md(message) == "> a\n> b"


def test_giphy_image_and_context_blocks():
    assert _md(_recorded(GIPHY)) == (
        "[image: some men just want to watch the world burn]"
        "(https://media4.giphy.com/media/F9yAvk7Xpr0c/giphy.gif"
        "?cid=6104955ee4adf136dcbbrjmn0qdsjgxzsga2bbxafvz2c6fe&ep=v1_gifs_translate&rid=giphy.gif"
        "&ct=g)\n"
        "giphy logo Posted using /giphy"
    )


def test_layout_section_header_divider_actions():
    message = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Deploy done"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*ok* <@U0CA61XJF2R>"},
                "fields": [{"type": "plain_text", "text": "a"}, {"type": "mrkdwn", "text": "_b_"}],
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Approve"}},
                    {"type": "static_select"},
                ],
            },
            {"type": "fancy_new", "text": {"type": "mrkdwn", "text": "fallback"}},
        ]
    }
    assert _md(message) == "**Deploy done**\n**ok** @Jim\na\n_b_\n---\n[Approve]\nfallback"


def test_mrkdwn_text_without_blocks():
    message = {
        "text": "*bold* _it_ ~gone~ `a*b` <https://x.test|site> <@U0CAC5YG4NS> &lt;tag&gt;\n"
        "```\n*not bold*\n```\n>>> quoted\nrest"
    }
    assert _md(message) == (
        "**bold** _it_ ~~gone~~ `a*b` [site](https://x.test) @Tim Naleid <tag>\n"
        "```\n*not bold*\n```\n> quoted\n> rest"
    )


def test_join_and_unknown_subtypes():
    joined = {"subtype": "channel_join", "text": "<@U0CAC5YG4NS> has joined the channel"}
    assert _md(joined) == "@Tim Naleid has joined the channel"
    assert _md({"subtype": "some_app_event", "text": "did *x*"}) == "did **x**"
    assert _md({"subtype": "some_app_event"}) == "some_app_event"
    assert _md({}) == ""


def test_every_recorded_message_renders():
    for line in recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl"):
        if line["meta"]["kind"] == "message":
            assert isinstance(_md(line["object"]), str)
