# ABOUTME: Tests for the search use case over DuckDB views of a lake of the recorded archive.
# ABOUTME: Covers case-insensitive matching, the --in and --from filters, ordering and permalinks.
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.domain.envelope import Envelope, Kind, Meta
from slarc.domain.links import MessageTs
from slarc.domain.window import Window
from slarc.ports import SearchHit, SlarcError
from slarc.service.search import search
from tests.contract.test_duckdb_index import (
    LATER,
    LATER_RUN,
    VERSION,
    commit,
    deleted,
    later_run,
    scalar,
)
from tests.fakes import FakeArchiveLock
from tests.recorded_fixtures import recorded_lake

ALL_NALEID = "C0CA5396G03"
FIXTURES = "C0CA7SNHTSP"
ARCHIVED = "C0CAF9YVDMY"
DOMAIN = "naleidworkspace"
TEAM = "T0CB5LYPVFA"
OTHER_TEAM = "T0OTHER"
TED = "U0CA538UEN7"
JIM = "U0CA61XJF2R"


def message_line(channel: str, ts: str, text: str, team: str = TEAM) -> Envelope:
    meta = Meta(
        kind="message",
        team=team,
        channel=channel,
        ts=ts,
        fetched_at=LATER,
        run=LATER_RUN,
        slarc=VERSION,
        hash=ts.replace(".", "").ljust(64, "0"),
        plain=f"Jim: {text}",
    )
    return Envelope(meta=meta, object={"type": "message", "user": JIM, "text": text, "ts": ts})


def directory_line(kind: Kind, id: str, fields: dict) -> Envelope:
    meta = Meta(
        kind=kind,
        team=TEAM,
        id=id,
        fetched_at=LATER,
        run=LATER_RUN,
        slarc=VERSION,
        hash=id.ljust(64, "0"),
    )
    return Envelope(meta=meta, object={"id": id, **fields})


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return recorded_lake(tmp_path / "archive")


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory, ext_dir: Path) -> Iterator[DuckDbQueryIndex]:
    """An index over one recorded lake that the module's read-only tests share."""
    root = recorded_lake(tmp_path_factory.mktemp("recorded") / "archive")
    with DuckDbQueryIndex(root, ext_dir, FakeArchiveLock()) as idx:
        yield idx


def channels(hits: list[SearchHit]) -> list[str]:
    return [hit.link.split("/archives/")[1].split("/")[0] for hit in hits]


def test_search_finds_plain_text_case_insensitive(index: DuckDbQueryIndex) -> None:
    hits = search(index, "SEED: Archived CHANNEL", None, None).hits
    assert [hit.ts for hit in hits] == [
        "1792437061.341469",
        "1792437061.072929",
        "1792437060.771479",
    ]
    first = hits[0]
    assert first.author == "Ted Naleid"
    assert first.conversation == "#slarc-fixtures-archived"
    assert "seed: archived channel message 3" in first.snippet
    limited = search(index, "seed: archived channel", None, None, limit=2)
    assert [hit.ts for hit in limited.hits] == ["1792437061.341469", "1792437061.072929"]


def test_search_treats_wildcards_literally(index: DuckDbQueryIndex) -> None:
    assert search(index, "seed%message", None, None).hits == []
    assert search(index, "seed_ archived", None, None).hits == []


def test_search_skips_deleted_messages(archive: Path, ext_dir: Path) -> None:
    commit(archive, ext_dir, deleted(ARCHIVED, "1792437061.341469", "2026-10-20T00:00:00Z"))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        hits = search(index, "seed: archived channel", None, None).hits
    assert [hit.ts for hit in hits] == ["1792437061.072929", "1792437060.771479"]


def test_search_in_and_from_filters(index: DuckDbQueryIndex) -> None:
    joined = search(index, "has joined the channel", None, None).hits
    assert sorted(set(channels(joined))) == [ALL_NALEID, FIXTURES, ARCHIVED]

    in_name = search(index, "has joined the channel", "#all-naleid", None).hits
    assert len(in_name) == 3
    assert set(channels(in_name)) == {ALL_NALEID}
    assert channels(search(index, "has joined the channel", FIXTURES, None).hits) == [FIXTURES] * 3

    for who in ("jim", "JIM", "@jim", "Jim Naleid", "U0CA61XJF2R"):
        hits = search(index, "has joined the channel", None, who).hits
        assert {hit.author for hit in hits} == {"Jim"}, who
        assert sorted(channels(hits)) == [ALL_NALEID, FIXTURES], who

    both = search(index, "has joined the channel", "#slarc-fixtures", "tim naleid").hits
    assert [(hit.author, hit.conversation) for hit in both] == [("Tim Naleid", "#slarc-fixtures")]


def test_search_without_any_filter_or_text_is_an_error(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError, match="Give text to search for, or a filter"):
        search(index, "  ", None, None)
    with pytest.raises(SlarcError, match="Give text to search for, or a filter"):
        search(index, None, None, None)


def test_search_in_dm_user(index: DuckDbQueryIndex) -> None:
    for who in ("@jim", "@Jim Naleid", f"@{JIM}"):
        hits = search(index, None, who, None).hits
        assert {hit.conversation for hit in hits} == {"@Jim"}, who
        assert any("direct message to jim" in hit.text.lower() for hit in hits), who


def test_search_without_text_lists_the_window(index: DuckDbQueryIndex) -> None:
    every = search(index, None, "#all-naleid", None, limit=0).hits
    assert len(every) == scalar(
        index,
        f"SELECT count(*) FROM message WHERE channel_id = '{ALL_NALEID}' AND deleted_at IS NULL",
    )
    assert [hit.ts for hit in every] == sorted((hit.ts for hit in every), reverse=True)


def test_search_since_until_bound_posting_time(index: DuckDbQueryIndex) -> None:
    every = search(index, None, "#all-naleid", None, limit=0).hits
    middle = every[len(every) // 2]
    window = Window(since=middle.ts)
    newer = search(index, None, "#all-naleid", None, window=window, limit=0).hits
    assert newer == [hit for hit in every if MessageTs(hit.ts) > middle.ts]
    older = search(index, None, "#all-naleid", None, window=Window(until=middle.ts), limit=0)
    assert older.hits == [hit for hit in every if MessageTs(hit.ts) < middle.ts]


def test_search_limit_reports_more(index: DuckDbQueryIndex) -> None:
    every = search(index, None, "#all-naleid", None, limit=0)
    assert not every.more
    cut = search(index, None, "#all-naleid", None, limit=2)
    assert (cut.hits, cut.more) == (every.hits[:2], True)
    whole = search(index, None, "#all-naleid", None, limit=len(every.hits))
    assert (whole.hits, whole.more) == (every.hits, False)


def test_search_hit_carries_readable_fields(index: DuckDbQueryIndex) -> None:
    [top] = search(index, "slarc seed v1", None, None).hits
    assert (top.team_id, top.workspace, top.channel_id) == (TEAM, DOMAIN, FIXTURES)
    assert top.text == "slarc seed v1"
    assert (top.author, top.author_real_name) == ("Ted Naleid", "Ted Naleid")
    [jim] = search(index, "reply from jim", "#all-naleid", None).hits
    assert (jim.author, jim.author_real_name) == ("Jim", "Jim Naleid")
    assert top.posted_at == datetime.fromtimestamp(1792437061.952039, UTC)
    assert top.thread_ts is None


def test_search_mentions_user_and_their_usergroups(archive: Path, ext_dir: Path) -> None:
    group = directory_line("usergroup", "S0ADMINS", {"handle": "admins", "users": [TED]})
    # A group whose id starts with S0ADMINS but which Ted is not in.
    other = directory_line("usergroup", "S0ADMINS2", {"handle": "others", "users": [JIM]})
    commit(
        archive,
        ext_dir,
        group,
        other,
        message_line(ALL_NALEID, "1792500004.000004", "ping <!subteam^S0ADMINS2|@others>"),
        message_line(ALL_NALEID, "1792500001.000001", f"hey <@{TED}> look"),
        message_line(ALL_NALEID, "1792500002.000002", "ping <!subteam^S0ADMINS|@admins>"),
        message_line(ALL_NALEID, "1792500003.000003", f"thanks <@{JIM}>"),
    )
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        for who in ("contact", "Ted Naleid", TED, "@contact", "me"):
            hits = search(index, None, None, None, mentions=who).hits
            assert [hit.ts for hit in hits] == ["1792500002.000002", "1792500001.000001"], who
        assert [hit.ts for hit in search(index, "look", None, None, mentions="contact").hits] == [
            "1792500001.000001"
        ]


def test_mentions_me_resolves_per_workspace(archive: Path, ext_dir: Path) -> None:
    auth = {"team_id": OTHER_TEAM, "user_id": "U0X", "domain": "other", "auth_test_at": LATER}
    commit(
        archive,
        ext_dir,
        message_line(ALL_NALEID, "1792500001.000001", f"hey <@{TED}>"),
        message_line("C0OTHER", "1792500002.000002", "hey <@U0X>", team=OTHER_TEAM),
        message_line("C0OTHER", "1792500003.000003", f"hey <@{TED}>", team=OTHER_TEAM),
        run=later_run(auth=[auth]),
    )
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        hits = search(index, None, None, None, mentions="me").hits
    assert [(hit.team_id, hit.ts) for hit in hits] == [
        (OTHER_TEAM, "1792500002.000002"),
        (TEAM, "1792500001.000001"),
    ]


def test_search_from_me_is_the_auth_user(index: DuckDbQueryIndex) -> None:
    mine = search(index, None, None, "me", limit=0).hits
    assert mine
    assert {hit.author for hit in mine} == {"Ted Naleid"}


def test_search_in_rejects_a_workspace_name(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError, match="--in takes #channel-name, @user or a conversation id"):
        search(index, "seed", "acme", None)


def test_search_hit_link_is_slack_permalink(index: DuckDbQueryIndex) -> None:
    [top] = search(index, "slarc seed v1", None, None).hits
    assert top.link == f"https://{DOMAIN}.slack.com/archives/{FIXTURES}/p1792437061952039"

    [reply] = search(index, "reply from jim", "#all-naleid", None).hits
    assert reply.link == (
        f"https://{DOMAIN}.slack.com/archives/{ALL_NALEID}/p1792427577619569"
        f"?thread_ts=1792427542.806979&cid={ALL_NALEID}"
    )

    [dm] = search(index, "direct message to jim", None, None).hits
    assert dm.conversation == "@Jim"


def test_search_names_that_match_nothing_are_errors(index: DuckDbQueryIndex) -> None:
    cases = [
        (("#nope", None, None), "No conversation named #nope in the archive. See slarc list."),
        (
            ("C0NOPE00000", None, None),
            "No conversation named C0NOPE00000 in the archive. See slarc list.",
        ),
        (("@nobody", None, None), "No DM with @nobody in the archive. See slarc list."),
        ((None, "nobody", None), "No user named nobody in the archive."),
        ((None, None, "@nobody"), "No user named nobody in the archive."),
    ]
    for (in_conv, from_user, mentions), message in cases:
        with pytest.raises(SlarcError) as raised:
            search(index, "seed", in_conv, from_user, mentions=mentions)
        assert str(raised.value) == message


def test_search_known_names_without_hits_are_no_match(index: DuckDbQueryIndex) -> None:
    assert search(index, "no such words", "#all-naleid", "jim", mentions="contact").hits == []
    assert search(index, "no such words", "@jim", None).hits == []
