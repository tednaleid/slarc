# ABOUTME: Tests for the search use case over DuckDB views of a copy of the recorded archive.
# ABOUTME: Covers case-insensitive matching, the --in and --from filters, ordering and permalinks.
from collections.abc import Iterator
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.domain.envelope import Envelope, Meta, encode_line
from slarc.ports import SearchHit, SlarcError
from slarc.service.search import search
from tests.recorded_fixtures import RECORDED_TEAM, copy_recorded

ALL_NALEID = "C0CA5396G03"
FIXTURES = "C0CA7SNHTSP"
ARCHIVED = "C0CAF9YVDMY"
DOMAIN = "naleidworkspace"


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return copy_recorded(tmp_path / "archive")


@pytest.fixture
def index(archive: Path) -> Iterator[DuckDbQueryIndex]:
    with DuckDbQueryIndex(archive) as idx:
        yield idx


def channels(hits: list[SearchHit]) -> list[str]:
    return [hit.link.split("/archives/")[1].split("/")[0] for hit in hits]


def test_search_finds_plain_text_case_insensitive(index: DuckDbQueryIndex) -> None:
    hits = search(index, "SEED: Archived CHANNEL", None, None)
    assert [hit.ts for hit in hits] == [
        "1792437061.341469",
        "1792437061.072929",
        "1792437060.771479",
    ]
    first = hits[0]
    assert first.author == "Ted Naleid"
    assert first.conversation == "#slarc-fixtures-archived"
    assert "seed: archived channel message 3" in first.snippet
    assert [hit.ts for hit in search(index, "seed: archived channel", None, None, limit=2)] == [
        "1792437061.341469",
        "1792437061.072929",
    ]


def test_search_treats_wildcards_literally(index: DuckDbQueryIndex) -> None:
    assert search(index, "seed%message", None, None) == []
    assert search(index, "seed_ archived", None, None) == []


def test_search_skips_deleted_messages(archive: Path) -> None:
    meta = Meta(
        kind="deleted",
        team=RECORDED_TEAM,
        channel=ARCHIVED,
        ts="1792437061.341469",
        fetched_at="2026-10-20T00:00:00Z",
        run="r_20261020T000000Z",
        slarc="0.1.0",
    )
    with (archive / "raw" / RECORDED_TEAM / ARCHIVED / "2026-10.jsonl").open("ab") as f:
        f.write(encode_line(Envelope(meta=meta)))
    with DuckDbQueryIndex(archive) as index:
        hits = search(index, "seed: archived channel", None, None)
    assert [hit.ts for hit in hits] == ["1792437061.072929", "1792437060.771479"]


def test_search_in_and_from_filters(index: DuckDbQueryIndex) -> None:
    joined = search(index, "has joined the channel", None, None)
    assert sorted(set(channels(joined))) == [ALL_NALEID, FIXTURES, ARCHIVED]

    in_name = search(index, "has joined the channel", "#all-naleid", None)
    assert len(in_name) == 3
    assert set(channels(in_name)) == {ALL_NALEID}
    assert channels(search(index, "has joined the channel", FIXTURES, None)) == [FIXTURES] * 3

    for who in ("jim", "JIM", "@jim", "Jim Naleid", "U0CA61XJF2R"):
        hits = search(index, "has joined the channel", None, who)
        assert {hit.author for hit in hits} == {"Jim"}, who
        assert sorted(channels(hits)) == [ALL_NALEID, FIXTURES], who

    both = search(index, "has joined the channel", "#slarc-fixtures", "tim naleid")
    assert [(hit.author, hit.conversation) for hit in both] == [("Tim Naleid", "#slarc-fixtures")]


def test_search_rejects_unreadable_in_and_empty_text(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError, match="--in"):
        search(index, "seed", "@jim", None)
    with pytest.raises(SlarcError, match="search"):
        search(index, "  ", None, None)


def test_search_hit_link_is_slack_permalink(index: DuckDbQueryIndex) -> None:
    [top] = search(index, "slarc seed v1", None, None)
    assert top.link == f"https://{DOMAIN}.slack.com/archives/{FIXTURES}/p1792437061952039"

    [reply] = search(index, "reply from jim", "#all-naleid", None)
    assert reply.link == (
        f"https://{DOMAIN}.slack.com/archives/{ALL_NALEID}/p1792427577619569"
        f"?thread_ts=1792427542.806979&cid={ALL_NALEID}"
    )

    [dm] = search(index, "direct message to jim", None, None)
    assert dm.conversation == "@Jim"
