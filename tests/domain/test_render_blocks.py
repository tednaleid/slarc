# ABOUTME: Tests for render_message_html: rich_text and layout blocks, mrkdwn fallback, subtypes.
# ABOUTME: Golden tests render every recorded message to tests/golden/ (--update-golden rewrites).
from pathlib import Path

import pytest

from slarc.domain.render.blocks import render_message_html
from tests.fakes import FakeAssetResolver
from tests.recorded_fixtures import RECORDED_TEAM, recorded_lines, recorded_view

GOLDEN = Path(__file__).parent.parent / "golden"
SEED_RICH_TEXT = "1792437053.095289"
GIPHY = "1792411464.844209"


def _html(message: dict) -> str:
    return render_message_html(message, recorded_view(), FakeAssetResolver())


def _recorded(ts: str) -> dict:
    return next(line["object"] for line in _message_lines() if line["meta"]["ts"] == ts)


def _message_lines() -> list[dict]:
    lines = recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    return [line for line in lines if line["meta"]["kind"] == "message"]


def _section(*elements: dict) -> dict:
    return {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_section", "elements": list(elements)}],
            }
        ]
    }


def test_rich_text_styles_and_nested_lists():
    html = _html(_recorded(SEED_RICH_TEXT))
    assert "<strong>bold</strong>, <em>italic</em>, <s>struck out</s>" in html
    assert "<code>inline code</code>" in html
    assert (
        "<ul><li>first bullet</li><li>second bullet<ul><li>nested bullet</li></ul></li>"
        "<li>third bullet</li></ul>"
    ) in html
    assert "<ol><li>step one</li><li>step two<ol><li>nested step</li></ol></li></ol>" in html


def test_rich_text_combined_styles_and_list_offset():
    message = _section(
        {
            "type": "text",
            "text": "x",
            "style": {
                "bold": True,
                "italic": True,
                "strike": True,
                "underline": True,
                "highlight": True,
                "code": True,
            },
        },
        {"type": "text", "text": "a\nb"},
    )
    assert _html(message) == (
        "<p><strong><em><s><u><mark><code>x</code></mark></u></s></em></strong>a<br>b</p>"
    )
    listed = {
        "blocks": [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "style": "ordered",
                        "indent": 0,
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
    assert _html(listed) == '<ol start="3"><li>c</li></ol>'


def test_rich_text_quote_and_preformatted():
    html = _html(_recorded(SEED_RICH_TEXT))
    assert "<blockquote>a quote</blockquote>" in html
    assert "<pre>def hello():\n    return &#39;world&#39;</pre>" in html


def test_user_and_channel_mentions_resolved():
    html = _html(_recorded(SEED_RICH_TEXT))
    assert '<span class="mention">@Jim</span>' in html
    assert '<span class="mention">#slarc-fixtures</span>' in html
    assert '<span class="mention">@here</span>' in html
    assert '<a href="https://example.com/docs">the docs</a>' in html
    unknown = _section(
        {"type": "user", "user_id": "UNKNOWN"}, {"type": "usergroup", "usergroup_id": "S9"}
    )
    assert _html(unknown) == (
        '<p><span class="mention">@UNKNOWN</span><span class="mention">@S9</span></p>'
    )


def test_rich_text_emoji_elements():
    message = _section(
        {"type": "emoji", "name": "teamwork2"},
        {"type": "emoji", "name": "not_in_table_yet", "unicode": "1fae9"},
        {"type": "emoji", "name": "gone_custom"},
    )
    assert _html(message) == (
        '<p><img class="emoji" src="assets/emoji/teamwork" alt=":teamwork2:" title=":teamwork2:">'
        f'<span class="emoji" title=":not_in_table_yet:">{chr(0x1FAE9)}</span>'
        ":gone_custom:</p>"
    )


def test_unsafe_link_renders_as_text():
    message = _section({"type": "link", "url": "javascript:alert(1)", "text": "<click>"})
    assert _html(message) == "<p>&lt;click&gt;</p>"


def test_giphy_image_and_context_blocks():
    assert _html(_recorded(GIPHY)) == (
        '<figure class="image">'
        '<img src="assets/images/giphy.gif" alt="some men just want to watch the world burn"'
        ' width="500" height="195">'
        "<figcaption>some men just want to watch the world burn</figcaption></figure>\n"
        '<div class="context"><img class="context-image" src="assets/images/service_32.png"'
        ' alt="giphy logo"> <span>Posted using /giphy</span></div>'
    )


def test_image_without_local_copy_links_to_source():
    class NoImages(FakeAssetResolver):
        def image_src(self, url: str) -> str | None:
            return None

    message = {"blocks": [{"type": "image", "image_url": "https://g.test/a.gif", "alt_text": "a"}]}
    html = render_message_html(message, recorded_view(), NoImages())
    assert html == '<figure class="image"><a href="https://g.test/a.gif">a</a></figure>'


def test_layout_section_header_divider_actions():
    message = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Deploy <done>"}},
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
        ]
    }
    assert _html(message) == (
        '<div class="header">Deploy &lt;done&gt;</div>\n'
        '<div class="section"><strong>ok</strong> <span class="mention">@Jim</span>'
        '<div class="fields"><div>a</div><div><em>b</em></div></div></div>\n'
        "<hr>\n"
        '<div class="actions"><span class="action">Approve</span></div>'
    )


def test_unknown_block_type_renders_escaped_text():
    message = {
        "blocks": [
            {"type": "fancy_new", "text": {"type": "mrkdwn", "text": "<b>hi</b> & bye"}},
            {"type": "rich_text", "elements": [{"type": "rich_text_mystery", "text": "<m>"}]},
            {"type": "no_text_at_all"},
        ],
    }
    message["blocks"].append(
        _section({"type": "widget", "text": "<i>w</i>"}, {"type": "canvas"})["blocks"][0]
    )
    assert _html(message) == (
        '<div class="unknown">&lt;b&gt;hi&lt;/b&gt; &amp; bye</div>\n'
        '<div class="unknown">&lt;m&gt;</div>\n'
        '<div class="unknown"></div>\n'
        '<p><span class="unknown">&lt;i&gt;w&lt;/i&gt;</span><span class="unknown"></span></p>'
    )


def test_message_without_blocks_renders_mrkdwn_text():
    message = {"subtype": "channel_join", "text": "<@U0CAC5YG4NS> has joined the channel"}
    assert _html(message) == (
        '<div class="mrkdwn"><span class="mention">@Tim Naleid</span> has joined the channel</div>'
    )


def test_unknown_subtype_renders_muted_generic_message():
    message = {"subtype": "some_app_event", "text": "did *x*", "blocks": [{"type": "divider"}]}
    assert _html(message) == '<div class="generic-message">did <strong>x</strong></div>'
    assert _html({"subtype": "some_app_event"}) == (
        '<div class="generic-message">some_app_event</div>'
    )


@pytest.mark.parametrize(
    "line",
    _message_lines(),
    ids=lambda line: f"{line['meta']['channel']}-{line['meta']['ts']}",
)
def test_golden_recorded_messages(line: dict, update_golden: bool):
    path = GOLDEN / f"{line['meta']['channel']}-{line['meta']['ts']}.html"
    html = _html(line["object"]) + "\n"
    if update_golden:
        path.write_text(html, encoding="utf-8")
    assert path.exists(), f"{path.name} is missing; run `just test --update-golden`"
    assert html == path.read_text(encoding="utf-8")
