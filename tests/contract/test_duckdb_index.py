# ABOUTME: Tests for the DuckDB query index over a lake imported from the recorded test workspace.
# ABOUTME: Covers latest-wins views, deleted messages, imported duplicates, and `slarc sql`.
import json
from collections.abc import Iterator
from pathlib import Path

import msgspec
import pytest
from typer.testing import CliRunner

from slarc import bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.jsonl_import import import_jsonl
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.cli import app
from slarc.domain.envelope import Envelope, Meta
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem
from slarc.ports import Commit, SlarcError
from tests.fakes import FakeKeyring
from tests.recorded_fixtures import RECORDED_TEAM, copy_recorded, recorded_lake, recorded_lines

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
    with DuckDbQueryIndex(root, ext_dir) as idx:
        yield idx


def later_run(**fields) -> RunRecord:
    return RunRecord(run=LATER_RUN, command="pull", started=LATER, finished=LATER, **fields)


def commit(root: Path, ext_dir: Path, *lines: Envelope, run: RunRecord | None = None) -> None:
    """Appends lines to the archive's lake and commits them with a run."""
    store = DuckLakeArchiveStore(root, ext_dir)
    try:
        for env in lines:
            store.append(env)
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
        columns, rows = index.sql("SELECT channel_id, ts FROM message")
        assert columns == ["channel_id", "ts"]
        assert sorted(rows) == sorted(recorded_message_keys())
        query = f"SELECT plain, edited_ts FROM message WHERE ts = '{WAVE_PARENT}'"
        assert index.sql(query)[1] == [("edited text", "1792500000.000000")]


def test_message_version_counts_lines(archive: Path, ext_dir: Path) -> None:
    raw_lines = recorded_lines(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    commit(archive, ext_dir, edit(ALL_NALEID, WAVE_PARENT, "edited text"))
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
        before = index.conversation_counts()
    commit(
        archive,
        ext_dir,
        deleted(ALL_NALEID, WAVE_PARENT, LATER),
        deleted(FIXTURES, FIXTURES_TOP, EARLIER),
    )
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
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


def test_views_identical_after_importing_duplicate_lines(
    index: DuckDbQueryIndex, tmp_path: Path, ext_dir: Path
) -> None:
    before = snapshot(index)
    doubled = copy_recorded(tmp_path / "doubled")
    for path in doubled.rglob("*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        with path.open("a", encoding="utf-8") as f:
            f.writelines(reversed(lines))
    import_jsonl(doubled, ext_dir)
    with DuckDbQueryIndex(doubled, ext_dir) as index:
        assert snapshot(index) == before
    assert all(before[view] for view in VIEWS if view not in ("usergroup", "download"))


def test_garbage_line_in_month_file_is_not_imported(tmp_path: Path, ext_dir: Path) -> None:
    root = copy_recorded(tmp_path / "archive")
    with (root / "raw" / RECORDED_TEAM / ALL_NALEID / "2026-10.jsonl").open("a") as f:
        f.write('{"meta":{"schema":1,"kind":"mess\n')
    import_jsonl(root, ext_dir)
    with DuckDbQueryIndex(root, ext_dir) as index:
        assert sorted(index.sql("SELECT channel_id, ts FROM message")[1]) == sorted(
            recorded_message_keys()
        )


def test_views_exist_over_an_empty_lake(tmp_path: Path, ext_dir: Path) -> None:
    DuckLakeArchiveStore(tmp_path, ext_dir).close()
    with DuckDbQueryIndex(tmp_path, ext_dir) as index:
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
        query = (
            "SELECT team_id, domain, user_ids, credential_source, last_auth_test "
            "FROM workspace ORDER BY team_id"
        )
        assert index.sql(query)[1] == [
            ("T0CB5LYPVFA", "naleid-renamed", ["U0CA538UEN7", "U0OTHER"], "keychain", LATER),
            ("T0OTHER", "other", ["U0X"], "keychain", LATER),
        ]
        assert index.sql("SELECT name FROM workspace")[0] == ["name"]


def test_run_view_exposes_api_calls_and_threads_read_as_json(archive: Path, ext_dir: Path) -> None:
    counts = {
        "messages_seen": 3,
        "api_calls": {"auth.test": 1, "conversations.replies": 2},
        "threads_read": {"search": 1},
    }
    commit(archive, ext_dir, run=later_run(counts=counts))
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    store = DuckLakeArchiveStore(archive, ext_dir)
    store.drop_messages(RECORDED_TEAM, ALL_NALEID, {WAVE_PARENT})
    store.commit(Commit())
    store.close()
    commit(archive, ext_dir, envelope(parent))
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    with DuckDbQueryIndex(archive, ext_dir) as index:
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
    store = DuckLakeArchiveStore(tmp_path, ext_dir)
    store.commit(Commit(items=[item, removed], downloads=[state]))
    store.close()
    with DuckDbQueryIndex(tmp_path, ext_dir) as index:
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


def test_run_view_keeps_tracked_and_downloads_columns(archive: Path, ext_dir: Path) -> None:
    """Imported runs keep their lists; a new run's state is in the tables, so its are empty."""
    recorded_run = recorded_lines("runs.jsonl")[0]["object"]
    item = TrackedItem(team_id=RECORDED_TEAM, kind="conversation", channel=ALL_NALEID)
    state = DownloadState(
        team_id=RECORDED_TEAM, target_kind="file", target="F0PDF", status="pending"
    )
    store = DuckLakeArchiveStore(archive, ext_dir)
    store.commit(Commit(downloads=[state]))
    store.close()
    commit(archive, ext_dir, run=later_run(tracked=[item], downloads=[state]))
    with DuckDbQueryIndex(archive, ext_dir) as index:
        columns, rows = index.sql("SELECT run, tracked, downloads FROM run ORDER BY run")
        assert columns == ["run", "tracked", "downloads"]
        assert [(run, json.loads(t), json.loads(d)) for run, t, d in rows] == [
            (recorded_run["run"], recorded_run["tracked"], recorded_run["downloads"]),
            (LATER_RUN, [], []),
        ]
        query = "SELECT target, status FROM download WHERE target = 'F0PDF'"
        assert index.sql(query)[1] == [("F0PDF", "pending")]


def test_bad_sql_raises_one_line_slarc_error(index: DuckDbQueryIndex) -> None:
    with pytest.raises(SlarcError) as caught:
        index.sql("SELEC 1")
    assert "syntax error" in str(caught.value)
    assert "\n" not in str(caught.value)


def cli_services(home: Path) -> bootstrap.Services:
    return bootstrap.Services(
        credentials=CompositeSource([]),
        keychain=KeyringSource(FakeKeyring()),
        gateway_factory=lambda _: pytest.fail("sql never connects to Slack"),
        env={},
        home=home,
    )


def run_sql(home: Path, *args: str):
    with bootstrap.override(cli_services(home)):
        return runner.invoke(app, ["sql", *args])


def test_cli_sql_prints_rows(archive: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(archive / "raw")
    result = run_sql(tmp_path, "SELECT id, display FROM user ORDER BY id")
    assert result.exit_code == 0, result.output
    assert "display" in result.output
    assert "U0CA61XJF2R" in result.output
    assert "Jim" in result.output


def test_cli_sql_uses_root_flag(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, "SELECT count(*) AS n FROM conversation", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "5" in result.output


def test_cli_sql_without_archive_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_sql(tmp_path, "SELECT 1")
    assert result.exit_code == 1
    assert "No slarc archive found" in result.output
    assert "Traceback" not in result.output


def test_cli_sql_bad_query_prints_error_line(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, "SELECT * FROM nothere", "--root", str(archive))
    assert result.exit_code == 1
    assert "nothere" in result.output
    assert result.output.strip().count("\n") == 0
    assert "Traceback" not in result.output


def test_cli_sql_column_names_are_not_markup(archive: Path, tmp_path: Path) -> None:
    result = run_sql(tmp_path, """SELECT 'a' AS "x[/]" """, "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "x[/]" in result.output
