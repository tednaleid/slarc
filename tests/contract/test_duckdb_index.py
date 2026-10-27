# ABOUTME: Tests for the DuckDB query index over a lake loaded from the recorded test workspace.
# ABOUTME: Covers latest-wins views, deleted messages, repeated rows, lake errors and `slarc sql`.
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import msgspec
import pytest
from typer.testing import CliRunner

from slarc import bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import CATALOG
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.cli import app
from slarc.domain.envelope import Envelope, Meta
from slarc.domain.links import message_link
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_from_envelope
from slarc.ports import ArchiveReadFailed, Commit, SearchFilter, SlarcError
from tests.adapters.test_lake_store import assert_one_plain_sentence
from tests.fakes import FakeArchiveLock, FakeKeyring
from tests.jsonl_loader import jsonl_envelopes
from tests.recorded_fixtures import RECORDED, RECORDED_TEAM, recorded_lake, recorded_lines

ALL_NALEID = "C0CA5396G03"
FIXTURES = "C0CA7SNHTSP"
WAVE_PARENT = "1792410693.663869"  # #all-naleid thread parent with wave reactions and 3 replies
FIXTURES_TOP = "1792437061.952039"  # #slarc-fixtures top-level message
LATER = "2026-10-20T00:00:00Z"
EARLIER = "2026-10-01T00:00:00Z"
LATER_RUN = "r_20261020T000000Z"
VERSION = "0.1.0"

VIEWS = [
    "message",
    "message_readable",
    "message_version",
    "reaction",
    "file",
    "message_file",
    "image",
    "user",
    "user_snapshot",
    "bot",
    "emoji",
    "usergroup",
    "conversation",
    "run",
    "workspace",
    "tracked_item",
    "download",
]

runner = CliRunner()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return recorded_lake(tmp_path / "archive")


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory, ext_dir: Path) -> Iterator[DuckDbQueryIndex]:
    """An index over one recorded lake that the module's read-only tests share."""
    root = recorded_lake(tmp_path_factory.mktemp("recorded") / "archive")
    with DuckDbQueryIndex(root, ext_dir, FakeArchiveLock()) as idx:
        yield idx


def later_run(**fields) -> RunRecord:
    return RunRecord(run=LATER_RUN, command="pull", started=LATER, finished=LATER, **fields)


def commit(root: Path, ext_dir: Path, *lines: Envelope, run: RunRecord | None = None) -> None:
    """Appends lines to the archive's lake, commits them, then records a run."""
    store = DuckLakeArchiveStore(root, ext_dir, FakeArchiveLock())
    try:
        for env in lines:
            store.append(env)
        store.commit(Commit())
        store.append_run(run or later_run(), VERSION)
    finally:
        store.close()


def envelope(line: dict) -> Envelope:
    return msgspec.convert(line, Envelope)


def stored_line(channel: str, ts: str) -> dict:
    lines = recorded_lines(f"raw/{RECORDED_TEAM}/{channel}/*.jsonl")
    return next(line for line in lines if line["meta"]["ts"] == ts)


def edit(channel: str, ts: str, text: str, fetched_at: str = LATER) -> Envelope:
    line = stored_line(channel, ts)
    line["meta"] |= {"fetched_at": fetched_at, "run": LATER_RUN, "hash": "e" * 64, "plain": text}
    line["object"] |= {"text": text, "edited": {"user": "U0CA538UEN7", "ts": "1792500000.000000"}}
    return envelope(line)


def deleted(channel: str, ts: str, fetched_at: str) -> Envelope:
    meta = Meta(
        kind="deleted",
        team=RECORDED_TEAM,
        channel=channel,
        ts=ts,
        fetched_at=fetched_at,
        run=LATER_RUN,
        slarc=VERSION,
    )
    return Envelope(meta=meta)


def scalar(index: DuckDbQueryIndex, query: str):
    return index.sql(query)[1][0][0]


def recorded_message_keys() -> set[tuple[str, str]]:
    lines = recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    return {(line["meta"]["channel"], line["meta"]["ts"]) for line in lines}


def test_message_view_one_row_per_key(archive: Path, ext_dir: Path) -> None:
    commit(archive, ext_dir, edit(ALL_NALEID, WAVE_PARENT, "edited text"))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        columns, rows = index.sql("SELECT channel_id, ts FROM message")
        assert columns == ["channel_id", "ts"]
        assert sorted(rows) == sorted(recorded_message_keys())
        query = f"SELECT plain, edited_ts FROM message WHERE ts = '{WAVE_PARENT}'"
        assert index.sql(query)[1] == [("edited text", "1792500000.000000")]


def test_message_has_time_and_thread_columns(archive: Path, ext_dir: Path) -> None:
    commit(archive, ext_dir, edit(ALL_NALEID, WAVE_PARENT, "edited text"))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = (
            "SELECT posted_at = to_timestamp(1792410693.663869), "
            "edited_at = to_timestamp(1792500000), thread_root_ts, is_reply "
            f"FROM message WHERE ts = '{WAVE_PARENT}'"
        )
        assert index.sql(query)[1] == [(True, True, WAVE_PARENT, False)]
        replies = index.sql(
            "SELECT count(*), bool_and(thread_root_ts = thread_ts) FROM message "
            f"WHERE thread_ts = '{WAVE_PARENT}' AND is_reply"
        )[1]
        assert replies == [(3, True)]
        top = index.sql(
            f"SELECT thread_root_ts, is_reply FROM message WHERE ts = '{FIXTURES_TOP}'"
        )[1]
        assert top == [(FIXTURES_TOP, False)]


def test_readable_text_strips_the_author_prefix(archive: Path, ext_dir: Path) -> None:
    commit(archive, ext_dir, edit(ALL_NALEID, WAVE_PARENT, "Dr: Who: says hello"))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = f"SELECT text FROM message_readable WHERE ts = '{FIXTURES_TOP}'"
        assert index.sql(query)[1] == [("slarc seed v1",)]
        # A display name holding ": " loses only what comes before its first ": ".
        query = f"SELECT text FROM message_readable WHERE ts = '{WAVE_PARENT}'"
        assert index.sql(query)[1] == [("Who: says hello",)]


def test_readable_names_author_and_conversation(index: DuckDbQueryIndex) -> None:
    query = (
        f"SELECT workspace, conversation, author FROM message_readable WHERE ts = '{FIXTURES_TOP}'"
    )
    assert index.sql(query)[1] == [("naleidworkspace", "#slarc-fixtures", "Ted Naleid")]
    names = index.sql(
        "SELECT DISTINCT author, author_real_name FROM message_readable WHERE author = 'Jim'"
    )[1]
    assert names == [("Jim", "Jim Naleid")]
    columns = index.sql("SELECT * FROM message_readable LIMIT 0")[0]
    assert columns == [
        "team_id",
        "workspace",
        "channel_id",
        "conversation",
        "ts",
        "thread_ts",
        "posted_at",
        "author",
        "author_real_name",
        "text",
        "link",
        "reactions",
        "files",
        "deleted_at",
        "first_seen_at",
    ]


def test_readable_reactions_and_files(index: DuckDbQueryIndex) -> None:
    query = "SELECT reactions, files FROM message_readable WHERE ts = '{}'"
    assert index.sql(query.format("1792410693.663869"))[1] == [
        (":wave::skin-tone-3: 1, :wave: 1", None)
    ]
    assert index.sql(query.format("1792422623.464629"))[1] == [
        (None, [{"id": "F0CB7HWPUTA", "name": "CleanShot 2026-10-19 at 10.09.44.png"}])
    ]


def test_every_view_has_a_comment(index: DuckDbQueryIndex) -> None:
    rows = index.sql("SELECT view_name, comment FROM duckdb_views() WHERE NOT internal")[1]
    assert {name for name, _ in rows} >= set(VIEWS)
    assert [name for name, comment in rows if not comment] == []


def test_readable_columns_have_comments(index: DuckDbQueryIndex) -> None:
    rows = index.sql(
        "SELECT column_name, comment FROM duckdb_columns() WHERE table_name = 'message_readable'"
    )[1]
    assert [name for name, comment in rows if not comment] == []


def test_sql_help_shows_examples(tmp_path: Path) -> None:
    result = run_sql(tmp_path, "--help")
    assert result.exit_code == 0, result.output
    assert "duckdb_views()" in result.stdout
    assert "FROM message_readable" in result.stdout


def test_readable_link_matches_message_link(index: DuckDbQueryIndex) -> None:
    rows = index.sql("SELECT workspace, channel_id, ts, thread_ts, link FROM message_readable")[1]
    assert rows
    for domain, channel, ts, thread_ts, link in rows:
        assert link == message_link(domain, channel, ts, thread_ts if thread_ts != ts else None)


def test_message_version_counts_lines(archive: Path, ext_dir: Path) -> None:
    raw_lines = recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    commit(archive, ext_dir, edit(ALL_NALEID, WAVE_PARENT, "edited text"))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        assert scalar(index, "SELECT count(*) FROM message_version") == len(raw_lines) + 1
        query = (
            "SELECT fetched_at, hash IS NOT NULL, edited_ts FROM message_version "
            f"WHERE ts = '{WAVE_PARENT}' ORDER BY fetched_at"
        )
        original = stored_line(ALL_NALEID, WAVE_PARENT)["meta"]["fetched_at"]
        assert index.sql(query)[1] == [
            (original, True, None),
            (LATER, True, "1792500000.000000"),
        ]


def test_deleted_at_set_from_deleted_line(archive: Path, ext_dir: Path) -> None:
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        before = index.conversation_counts()
    commit(
        archive,
        ext_dir,
        deleted(ALL_NALEID, WAVE_PARENT, LATER),
        deleted(FIXTURES, FIXTURES_TOP, EARLIER),
    )
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = f"SELECT deleted_at, plain FROM message WHERE ts = '{WAVE_PARENT}'"
        plain = stored_line(ALL_NALEID, WAVE_PARENT)["meta"]["plain"]
        assert index.sql(query)[1] == [(LATER, plain)]
        stale = f"SELECT deleted_at FROM message WHERE ts = '{FIXTURES_TOP}'"
        assert index.sql(stale)[1] == [(None,)]
        counts = index.conversation_counts()
        assert counts[(RECORDED_TEAM, ALL_NALEID)] == before[(RECORDED_TEAM, ALL_NALEID)] - 1
        assert counts[(RECORDED_TEAM, FIXTURES)] == before[(RECORDED_TEAM, FIXTURES)]
        shown = index.messages(RECORDED_TEAM, ALL_NALEID, None, None, include_deleted=False)
        assert WAVE_PARENT not in [m["ts"] for m in shown]
        everything = index.messages(RECORDED_TEAM, ALL_NALEID, None, None, include_deleted=True)
        parent = next(m for m in everything if m["ts"] == WAVE_PARENT)
        assert parent["deleted_at"] == LATER
        assert parent["object"]["ts"] == WAVE_PARENT


def test_reaction_base_name(index: DuckDbQueryIndex) -> None:
    query = (
        "SELECT name, base_name, count, user_ids FROM reaction "
        f"WHERE channel_id = '{ALL_NALEID}' AND ts = '{WAVE_PARENT}' ORDER BY name"
    )
    rows = index.sql(query)[1]
    assert [(name, base, count) for name, base, count, _ in rows] == [
        ("wave", "wave", 1),
        ("wave::skin-tone-3", "wave", 1),
    ]
    assert all(len(user_ids) == 1 for *_, user_ids in rows)


def test_user_display_fallback(archive: Path, ext_dir: Path) -> None:
    bare = {
        "meta": {
            "schema": 1,
            "kind": "user",
            "team": RECORDED_TEAM,
            "id": "U0BARE",
            "fetched_at": LATER,
            "run": LATER_RUN,
            "api": "users.list",
            "slarc": "0.1.0",
            "hash": "b" * 64,
        },
        "object": {"id": "U0BARE", "name": "bare", "profile": {"display_name": ""}},
    }
    commit(archive, ext_dir, envelope(bare))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        rows = index.sql("SELECT id, display FROM user ORDER BY id")[1]
        assert dict(rows) == {
            "U0BARE": "bare",
            "U0CA538UEN7": "Ted Naleid",
            "U0CA61XJF2R": "Jim",
            "U0CAC5YG4NS": "Tim Naleid",
            "USLACKBOT": "Slackbot",
        }


def snapshot(index: DuckDbQueryIndex) -> dict[str, list[tuple]]:
    return {view: index.sql(f"SELECT * FROM {view} ORDER BY ALL")[1] for view in VIEWS}


def test_views_other_than_message_version_ignore_repeated_rows(
    index: DuckDbQueryIndex, archive: Path, ext_dir: Path
) -> None:
    before = snapshot(index)
    lines = jsonl_envelopes(RECORDED, "raw/*/*/*.jsonl")
    lines += jsonl_envelopes(RECORDED, "directory/*/*.jsonl")
    recorded_run = run_from_envelope(jsonl_envelopes(RECORDED, "runs.jsonl")[0])
    commit(archive, ext_dir, *reversed(lines), run=recorded_run)
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as repeated:
        after = snapshot(repeated)
    versions = after.pop("message_version")
    assert after == {view: rows for view, rows in before.items() if view != "message_version"}
    assert sorted(versions) == sorted(before["message_version"] * 2)
    assert all(before[view] for view in VIEWS if view not in ("usergroup", "download"))


def test_views_exist_over_an_empty_lake(tmp_path: Path, ext_dir: Path) -> None:
    DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock()).close()
    with DuckDbQueryIndex(tmp_path, ext_dir, FakeArchiveLock()) as index:
        for view in VIEWS:
            columns, rows = index.sql(f"SELECT * FROM {view}")
            assert columns
            assert rows == []
        assert index.conversation_counts() == {}


def test_conversation_view_kinds(index: DuckDbQueryIndex) -> None:
    rows = index.sql("SELECT id, kind, name, dm_user_id FROM conversation ORDER BY id")[1]
    assert rows == [
        ("C0CA5396G03", "channel", "all-naleid", None),
        ("C0CA7SNHTSP", "channel", "slarc-fixtures", None),
        ("C0CAF9YVDMY", "channel", "slarc-fixtures-archived", None),
        ("C0CAG251117", "group_dm", "mpdm-tim--jim--contact-1", None),
        ("D0CAD7JE1QR", "dm", None, "U0CA61XJF2R"),
    ]


def test_workspace_view_takes_latest_auth_per_team(archive: Path, ext_dir: Path) -> None:
    later = {
        "team_id": RECORDED_TEAM,
        "user_id": "U0OTHER",
        "domain": "naleid-renamed",
        "credential_source": "keychain",
        "auth_test_at": LATER,
    }
    other = later | {"team_id": "T0OTHER", "user_id": "U0X", "domain": "other"}
    commit(archive, ext_dir, run=later_run(auth=[later, other]))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = (
            "SELECT team_id, domain, user_id, user_ids, credential_source, last_auth_test "
            "FROM workspace ORDER BY team_id"
        )
        assert index.sql(query)[1] == [
            (
                "T0CB5LYPVFA",
                "naleid-renamed",
                "U0OTHER",
                ["U0CA538UEN7", "U0OTHER"],
                "keychain",
                LATER,
            ),
            ("T0OTHER", "other", "U0X", ["U0X"], "keychain", LATER),
        ]
        assert index.sql("SELECT name FROM workspace")[0] == ["name"]


def test_run_view_exposes_api_calls_and_threads_read_as_json(archive: Path, ext_dir: Path) -> None:
    counts = {
        "messages_seen": 3,
        "api_calls": {"auth.test": 1, "conversations.replies": 2},
        "threads_read": {"search": 1},
    }
    commit(archive, ext_dir, run=later_run(counts=counts))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = (
            "SELECT messages_seen, CAST(api_calls ->> 'conversations.replies' AS BIGINT), "
            "CAST(threads_read ->> 'search' AS BIGINT), json_keys(api_calls) "
            f"FROM run WHERE run = '{LATER_RUN}'"
        )
        assert index.sql(query)[1] == [(3, 2, 1, ["auth.test", "conversations.replies"])]


def test_reply_count_computed_from_stored_replies(index: DuckDbQueryIndex) -> None:
    query = (
        "SELECT is_thread_parent, reply_count, latest_reply FROM message "
        f"WHERE ts = '{WAVE_PARENT}'"
    )
    assert index.sql(query)[1] == [(True, 3, "1792422623.464629")]


def test_parent_stored_before_its_replies_is_thread_parent(archive: Path, ext_dir: Path) -> None:
    parent = stored_line(ALL_NALEID, WAVE_PARENT)
    del parent["object"]["thread_ts"]
    store = DuckLakeArchiveStore(archive, ext_dir, FakeArchiveLock())
    store.drop_messages(RECORDED_TEAM, ALL_NALEID, {WAVE_PARENT})
    store.commit(Commit())
    store.close()
    commit(archive, ext_dir, envelope(parent))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        query = (
            "SELECT is_thread_parent, thread_ts, reply_count FROM message "
            f"WHERE ts = '{WAVE_PARENT}'"
        )
        assert index.sql(query)[1] == [(True, WAVE_PARENT, 3)]


def file_lines_differing_in_blob() -> list[dict]:
    meta = {
        "schema": 1,
        "kind": "file",
        "team": RECORDED_TEAM,
        "id": "F0BLOB",
        "fetched_at": LATER,
        "run": LATER_RUN,
        "api": "files.info",
        "slarc": "0.1.0",
        "hash": "f" * 64,
    }
    obj = {"id": "F0BLOB", "name": "notes.txt", "size": 5}
    return [{"meta": meta, "object": obj}, {"meta": meta | {"blob": "a" * 64}, "object": obj}]


@pytest.mark.parametrize("order", [1, -1])
def test_directory_tie_prefers_line_with_blob(archive: Path, ext_dir: Path, order: int) -> None:
    commit(archive, ext_dir, *map(envelope, file_lines_differing_in_blob()[::order]))
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        assert index.sql("SELECT blob FROM file WHERE id = 'F0BLOB'")[1] == [("a" * 64,)]


def test_conversation_counts(index: DuckDbQueryIndex) -> None:
    counts = index.conversation_counts()
    assert counts[(RECORDED_TEAM, ALL_NALEID)] == 14
    assert counts[(RECORDED_TEAM, "D0CAD7JE1QR")] == 1


def test_messages_ordered_by_ts_within_exclusive_range(index: DuckDbQueryIndex) -> None:
    every = index.messages(RECORDED_TEAM, ALL_NALEID, None, None, include_deleted=False)
    stamps = [m["ts"] for m in every]
    assert stamps == sorted(stamps, key=lambda ts: tuple(int(p) for p in ts.split(".")))
    assert all(isinstance(ts, str) for ts in stamps)
    bounded = index.messages(
        RECORDED_TEAM, ALL_NALEID, stamps[0], stamps[-1], include_deleted=False
    )
    assert [m["ts"] for m in bounded] == stamps[1:-1]
    first = every[0]
    for key in ("thread_ts", "object", "plain", "deleted_at", "edited_ts", "user_id"):
        assert key in first
    for key in ("bot_id", "subtype", "reply_count", "latest_reply"):
        assert key in first


def test_thread_returns_parent_then_replies(index: DuckDbQueryIndex) -> None:
    messages = index.thread(RECORDED_TEAM, ALL_NALEID, WAVE_PARENT, include_deleted=False)
    assert [m["ts"] for m in messages] == [
        WAVE_PARENT,
        "1792410743.787639",
        "1792411464.844209",
        "1792422623.464629",
    ]
    assert messages[0]["reply_count"] == 3


def message_view_rows(index: DuckDbQueryIndex, condition: str) -> list[tuple[str, str | None]]:
    query = f"SELECT ts, deleted_at FROM message WHERE {condition} ORDER BY ts"
    return [tuple(row) for row in index.sql(query)[1]]


def test_messages_and_thread_match_the_message_view(archive: Path, ext_dir: Path) -> None:
    reply = "1792411464.844209"
    commit(
        archive,
        ext_dir,
        edit(ALL_NALEID, WAVE_PARENT, "edited text"),
        deleted(ALL_NALEID, reply, LATER),
    )
    in_channel = f"team_id = '{RECORDED_TEAM}' AND channel_id = '{ALL_NALEID}'"
    in_thread = f"{in_channel} AND (ts = '{WAVE_PARENT}' OR thread_ts = '{WAVE_PARENT}')"
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        every = index.messages(RECORDED_TEAM, ALL_NALEID, None, None, include_deleted=True)
        thread = index.thread(RECORDED_TEAM, ALL_NALEID, WAVE_PARENT, include_deleted=True)
        shown = index.thread(RECORDED_TEAM, ALL_NALEID, WAVE_PARENT, include_deleted=False)

        assert [(m["ts"], m["deleted_at"]) for m in every] == message_view_rows(index, in_channel)
        assert [(m["ts"], m["deleted_at"]) for m in thread] == message_view_rows(index, in_thread)
        assert (reply, LATER) in [(m["ts"], m["deleted_at"]) for m in thread]
        assert reply not in [m["ts"] for m in shown]
        assert thread[0]["plain"] == "edited text"


def test_tracked_item_and_download_views(tmp_path: Path, ext_dir: Path) -> None:
    item = TrackedItem(
        team_id=RECORDED_TEAM,
        kind="conversation",
        channel=ALL_NALEID,
        newest_ts=WAVE_PARENT,
        last_read={"U0CA538UEN7": WAVE_PARENT},
    )
    removed = TrackedItem(
        team_id=RECORDED_TEAM, kind="thread", channel=FIXTURES, ts=FIXTURES_TOP, status="removed"
    )
    state = DownloadState(
        team_id=RECORDED_TEAM, target_kind="file", target="F0PDF", status="failed", attempts=2
    )
    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    store.commit(Commit(items=[item, removed], downloads=[state]))
    store.close()
    with DuckDbQueryIndex(tmp_path, ext_dir, FakeArchiveLock()) as index:
        rows = index.sql(
            "SELECT team_id, kind, channel_id, ts, newest_ts, status, "
            "last_read ->> 'U0CA538UEN7', json_array_length(retry_threads) "
            "FROM tracked_item ORDER BY kind"
        )[1]
        assert rows == [
            (RECORDED_TEAM, "conversation", ALL_NALEID, None, WAVE_PARENT, "ok", WAVE_PARENT, 0),
            (RECORDED_TEAM, "thread", FIXTURES, FIXTURES_TOP, None, "removed", None, 0),
        ]
        assert index.sql("SELECT * FROM tracked_item")[0] == [
            "team_id",
            "kind",
            "channel_id",
            "ts",
            "since",
            "until",
            "newest_ts",
            "oldest_ts",
            "backfill_complete",
            "last_search_at",
            "last_pull_at",
            "status",
            "archived",
            "last_read",
            "retry_threads",
        ]
        assert index.sql("SELECT * FROM download") == (
            ["team_id", "target_kind", "target", "url", "status", "attempts", "last_error"],
            [(RECORDED_TEAM, "file", "F0PDF", None, "failed", 2, None)],
        )


def test_run_view_leaves_out_tracked_and_downloads(archive: Path, ext_dir: Path) -> None:
    """Runs record that state in the tracked_item and download_state tables instead."""
    with DuckDbQueryIndex(archive, ext_dir, FakeArchiveLock()) as index:
        columns, rows = index.sql("SELECT * FROM run")
    assert rows
    assert "tracked" not in columns
    assert "downloads" not in columns


def test_sql_returns_timestamps_with_time_zones(index: DuckDbQueryIndex) -> None:
    columns, rows = index.sql("SELECT to_timestamp(1733392800) AS posted")
    assert columns == ["posted"]
    assert rows[0][0].timestamp() == 1733392800


def test_bad_sql_raises_one_line_slarc_error(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError) as caught:
        index.sql("SELEC 1")
    assert "syntax error" in str(caught.value)
    assert "\n" not in str(caught.value)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT user_name FROM message_readable",
        "SELECT datetime('now')",
        "SELECT * FROM messages_readable",
        "SELEC 1",
    ],
)
def test_sql_naming_or_syntax_errors_say_how_to_find_the_schema(
    index: DuckDbQueryIndex, query: str
) -> None:
    with pytest.raises(SlarcError) as caught:
        index.sql(query)
    message = str(caught.value)
    assert "slarc sql runs DuckDB SQL, not SQLite" in message
    assert "duckdb_views()" in message
    assert "duckdb_columns() WHERE table_name = 'message_readable'" in message
    assert "\n" not in message


def test_sql_errors_about_values_carry_no_schema_hint(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError) as caught:
        index.sql("SELECT CAST('x' AS INTEGER)")
    assert "duckdb_columns()" not in str(caught.value)


QUERIES: dict[str, Callable[[DuckDbQueryIndex], object]] = {
    "search": lambda index: index.search(SearchFilter(text="wave", limit=10)),
    "conversation_counts": lambda index: index.conversation_counts(),
    "messages": lambda index: index.messages(RECORDED_TEAM, ALL_NALEID, None, None, False),
    "thread": lambda index: index.thread(RECORDED_TEAM, ALL_NALEID, WAVE_PARENT, False),
}


@pytest.mark.parametrize("query", QUERIES.values(), ids=QUERIES.keys())
def test_a_lake_error_while_querying_is_an_archive_read_failure(
    tmp_path: Path, ext_dir: Path, query: Callable[[DuckDbQueryIndex], object]
) -> None:
    DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock()).close()
    with DuckDbQueryIndex(tmp_path, ext_dir, FakeArchiveLock()) as index:
        index._db.close()
        with pytest.raises(ArchiveReadFailed) as caught:
            query(index)
    assert_one_plain_sentence(str(caught.value), tmp_path / CATALOG)


def cli_services(home: Path) -> bootstrap.Services:
    return bootstrap.Services(
        credentials=CompositeSource([]),
        keychain=KeyringSource(FakeKeyring()),
        gateway_factory=lambda _, __: pytest.fail("sql never connects to Slack"),
        env={},
        home=home,
    )


def run_sql(home: Path, *args: str):
    with bootstrap.override(cli_services(home)):
        return runner.invoke(app, ["sql", *args])


def test_cli_sql_prints_rows(archive: Path, tmp_path: Path, monkeypatch) -> None:
    (archive / "sub").mkdir()
    monkeypatch.chdir(archive / "sub")
    result = run_sql(tmp_path, "SELECT id, display FROM user ORDER BY id")
    assert result.exit_code == 0, result.output
    assert "display" in result.output
    assert "U0CA61XJF2R" in result.output
    assert "Jim" in result.output


def test_cli_sql_uses_root_flag(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, "SELECT count(*) AS n FROM conversation", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "5" in result.output


def test_cli_sql_format_jsonl_is_parseable_and_untruncated(archive: Path, tmp_path: Path) -> None:
    query = "SELECT repeat('x', 500) AS t, 2 AS n"
    result = run_sql(tmp_path, query, "--format", "jsonl", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines()] == [{"t": "x" * 500, "n": 2}]


def test_cli_sql_without_archive_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_sql(tmp_path, "SELECT 1")
    assert result.exit_code == 2
    assert "No slarc archive found" in result.output
    assert "Traceback" not in result.output


def test_cli_sql_bad_query_prints_error_line(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, "SELECT * FROM nothere", "--root", str(archive))
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "nothere" in result.stderr
    assert result.output.strip().count("\n") == 0
    assert "Traceback" not in result.output


def test_cli_sql_column_names_are_not_markup(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, """SELECT 'a' AS "x[/]" """, "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "x[/]" in result.output
