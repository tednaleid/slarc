# ABOUTME: Tests for Slack link, selector and message timestamp parsing.
# ABOUTME: Covers channel and message permalinks, bare ids, names, users and ts ordering.
import pytest
from hypothesis import given
from hypothesis import strategies as st

from slarc.domain.links import (
    ConversationSel,
    MessageSel,
    MessageTs,
    WorkspaceSel,
    message_link,
    parse_selector,
)

BASE = "https://naleidworkspace.slack.com/archives/C0CA5396G03"


def test_channel_link():
    assert parse_selector(BASE) == ConversationSel(
        domain="naleidworkspace", channel="C0CA5396G03", name=None, dm_user=None
    )


def test_message_link():
    assert parse_selector(f"{BASE}/p1792410693663869") == MessageSel(
        domain="naleidworkspace",
        channel="C0CA5396G03",
        ts=MessageTs("1792410693.663869"),
        thread_ts=None,
    )


def test_reply_link_carries_thread_ts():
    sel = parse_selector(f"{BASE}/p1792422623464629?thread_ts=1792410693.663869&cid=C0CA5396G03")
    assert isinstance(sel, MessageSel)
    assert sel.ts == "1792422623.464629"
    assert sel.thread_ts == "1792410693.663869"


def test_bare_id():
    assert parse_selector("C0CAG251117") == ConversationSel(
        domain=None, channel="C0CAG251117", name=None, dm_user=None
    )


def test_hash_name():
    assert parse_selector("#all-naleid") == ConversationSel(
        domain=None, channel=None, name="all-naleid", dm_user=None
    )


def test_at_user():
    assert parse_selector("@jim") == ConversationSel(
        domain=None, channel=None, name=None, dm_user="jim"
    )


def test_rejects_non_slack_url():
    with pytest.raises(ValueError, match=r"https://example\.com/archives/C0CA5396G03"):
        parse_selector("https://example.com/archives/C0CA5396G03")


def test_rejects_short_p_digits():
    bad = f"{BASE}/p17924106936638"
    with pytest.raises(ValueError, match="p17924106936638"):
        parse_selector(bad)


@pytest.mark.parametrize("text", ["", "#", "@", "foo_bar", "-acme", "acme.slack.com"])
def test_rejects_unrecognized_text(text):
    with pytest.raises(ValueError, match="selector"):
        parse_selector(text)


@pytest.mark.parametrize("text", ["acme", "my-team2"])
def test_bare_word_is_a_workspace(text):
    assert parse_selector(text) == WorkspaceSel(domain=text)


def test_workspace_domain_is_lowercased():
    assert parse_selector("Acme") == WorkspaceSel(domain="acme")


def test_channel_id_is_not_a_workspace():
    assert parse_selector("C0CA5396G03") == ConversationSel(
        domain=None, channel="C0CA5396G03", name=None, dm_user=None
    )


def test_selector_error_lists_the_workspace_form():
    with pytest.raises(ValueError, match=r"or a workspace name\.$"):
        parse_selector("foo_bar")


def test_message_ts_ordering():
    assert MessageTs("1792410693.663869") < MessageTs("1792410700.000001")
    assert MessageTs("1792410693.663869") <= MessageTs("1792410693.663869")
    assert MessageTs("1792410700.000001") > MessageTs("1792410693.663869")


def test_message_ts_sorts_numerically():
    ts = [MessageTs("1792410693.100000"), MessageTs("999999999.5"), MessageTs("1792410693.99")]
    assert sorted(ts) == ["999999999.5", "1792410693.99", "1792410693.100000"]


def test_message_ts_equals_plain_string():
    assert MessageTs("1792410693.663869") == "1792410693.663869"
    assert hash(MessageTs("1792410693.663869")) == hash("1792410693.663869")


def test_message_ts_from_permalink_digits():
    assert MessageTs.from_permalink_digits("1792410693663869") == "1792410693.663869"


def test_message_link_builds_permalink():
    assert (
        message_link("naleidworkspace", "C0CA5396G03", "1792410693.663869")
        == f"{BASE}/p1792410693663869"
    )


def test_message_link_with_thread_ts():
    link = message_link("naleidworkspace", "C0CA5396G03", "1792422623.464629", "1792410693.663869")
    assert link == (f"{BASE}/p1792422623464629?thread_ts=1792410693.663869&cid=C0CA5396G03")


@given(
    seconds=st.integers(min_value=10**9, max_value=10**10 - 1),
    micros=st.integers(min_value=0, max_value=10**6 - 1),
)
def test_permalink_round_trip(seconds, micros):
    ts = f"{seconds}.{micros:06d}"
    sel = parse_selector(message_link("naleidworkspace", "C0CA5396G03", ts))
    assert isinstance(sel, MessageSel)
    assert sel.ts == ts
