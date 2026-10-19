# ABOUTME: Tests for render_mrkdwn_html, the HTML renderer for Slack mrkdwn text.
# ABOUTME: Covers styles, code, quotes, links, mentions, emoji shortcodes and escaping.
from slarc.domain.render.mrkdwn import render_mrkdwn_html
from tests.fakes import FakeAssetResolver
from tests.recorded_fixtures import recorded_view

WAVE_TONE_3 = chr(0x1F44B) + chr(0x1F3FC)


def _html(text: str) -> str:
    return render_mrkdwn_html(text, recorded_view(), FakeAssetResolver())


def test_mrkdwn_fallback_bold_italic_strike_code_links():
    text = (
        "*bold* _italic_ ~strike~ `a &lt;b&gt; *c*` "
        "<https://x.test/a?b=1&amp;c=2|the doc> <https://y.test> *bold <https://z.test|link>*"
    )
    assert _html(text) == (
        "<strong>bold</strong> <em>italic</em> <s>strike</s> <code>a &lt;b&gt; *c*</code> "
        '<a href="https://x.test/a?b=1&amp;c=2">the doc</a> '
        '<a href="https://y.test">https://y.test</a> '
        '<strong>bold <a href="https://z.test">link</a></strong>'
    )


def test_mrkdwn_markers_inside_words_stay_literal():
    assert _html("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4"


def test_mrkdwn_code_block_keeps_text_literal():
    assert _html("before\n```x = *1*\n  y &amp; z```\nafter") == (
        "before<br><pre>x = *1*\n  y &amp; z</pre><br>after"
    )


def test_mrkdwn_quotes():
    text = "&gt; quoted *line*\n&gt; second\nnormal\n&gt;&gt;&gt; rest\nof it"
    assert _html(text) == (
        "<blockquote>quoted <strong>line</strong><br>second</blockquote>"
        "normal<blockquote>rest<br>of it</blockquote>"
    )


def test_mrkdwn_mentions_resolved():
    text = "<@U0CA61XJF2R> in <#C0CA7SNHTSP> <!here> <@UNKNOWN> <!subteam^S1|@eng>"
    assert _html(text) == (
        '<span class="mention">@Jim</span> in <span class="mention">#slarc-fixtures</span> '
        '<span class="mention">@here</span> <span class="mention">@UNKNOWN</span> '
        '<span class="mention">@eng</span>'
    )


def test_mrkdwn_emoji_shortcodes():
    assert _html(":teamwork2: :wave::skin-tone-3: :nope: at 10:30:45") == (
        '<img class="emoji" src="assets/emoji/teamwork" alt=":teamwork2:" title=":teamwork2:"> '
        f'<span class="emoji" title=":wave::skin-tone-3:">{WAVE_TONE_3}</span> '
        ":nope: at 10:30:45"
    )


def test_mrkdwn_escapes_html():
    text = "&lt;script&gt;alert(1)&lt;/script&gt; \"q\" 'a' <javascript:alert(1)|click>"
    assert _html(text) == ("&lt;script&gt;alert(1)&lt;/script&gt; &#34;q&#34; &#39;a&#39; click")


def test_mrkdwn_escapes_link_url_and_label():
    assert _html('<https://x.test/"onmouseover=x|&lt;b&gt;&amp;&lt;/b&gt;>') == (
        '<a href="https://x.test/&#34;onmouseover=x">&lt;b&gt;&amp;&lt;/b&gt;</a>'
    )
