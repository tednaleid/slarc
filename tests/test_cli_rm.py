# ABOUTME: Tests for the `slarc rm` command over a lake of the recorded archive, without Slack.
# ABOUTME: One test pulls with the pull fixture to track a thread inside a tracked conversation.
import dataclasses
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.adapters.lake import CATALOG, DATA_DIR
from slarc.adapters.lake_store import VERSION_ORDER, DuckLakeArchiveStore
from slarc.cli import app
from slarc.domain.tracking import RunRecord
from slarc.ports import ArchiveStore, Commit, CommitConflict, MaintenanceFailed
from slarc.service.remove import Removal
from tests.adapters.test_locks import HOLDER
from tests.contract.test_duckdb_index import envelope
from tests.fakes import FakeArchiveLock
from tests.recorded_fixtures import RECORDED, RECORDED_TEAM, recorded_lake, recorded_lines
from tests.test_cli_list_search import services
from tests.test_cli_pull import (  # noqa: F401
    BASE,
    Fixture,
    f,
    lake_count,
    lake_queries,
    lake_rows,
)

runner = CliRunner()
ALL_NALEID = "C0CA5396G03"
CHANNEL_LINK = f"https://naleidworkspace.slack.com/archives/{ALL_NALEID}"
TABLES = ("message_version", "directory_entry", "run", "tracked_item", "download_state")
THREAD_LINK = f"{BASE}/p1792410693663869"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = recorded_lake(tmp_path / "archive")
    shutil.copytree(RECORDED / "blobs", root / "blobs")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def invoke(home: Path, *args: str, input: str | None = None):
    with bootstrap.override(services(home, [])):
        return runner.invoke(app, list(args), input=input)


def snapshot(root: Path, ext_dir: Path) -> dict[str, object]:
    """Every lake table's rows and every blob's bytes."""
    rows = lake_queries(root, ext_dir, *(f"SELECT * FROM {t} ORDER BY ALL" for t in TABLES))
    tables = dict(zip(TABLES, rows, strict=True))
    blobs = {p.relative_to(root): p.read_bytes() for p in root.rglob("blobs/**/*") if p.is_file()}
    return {"tables": tables, "blobs": blobs}


def channel_messages(root: Path, ext_dir: Path, channel: str) -> int:
    query = f"SELECT count(*) FROM message_version WHERE channel = '{channel}'"
    return lake_rows(root, ext_dir, query)[0][0]


def test_rm_prompts_and_deletes_on_yes(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = lake_count(archive, ext_dir, "message_version")
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="y\n")
    assert result.exit_code == 0, result.output
    assert "Stop tracking 1 item and delete " in result.output
    assert "Messages deleted from Slack since they were pulled cannot be pulled again." in (
        result.output
    )
    assert "[y/N]" in result.output
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0
    assert lake_count(archive, ext_dir, "message_version") < before
    assert result.output.strip().splitlines()[-1].startswith("Stopped tracking 1 item; deleted ")


def test_rm_declined_changes_nothing(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="n\n")
    assert result.exit_code == 2
    assert "Nothing removed." in result.output
    assert snapshot(archive, ext_dir) == before


def test_rm_default_answer_is_no(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="\n")
    assert result.exit_code == 2
    assert snapshot(archive, ext_dir) == before


def test_rm_aborted_prompt_changes_nothing(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="")
    assert result.exit_code == 2
    assert "Nothing removed." in result.output
    assert "Aborted!" not in result.output
    assert snapshot(archive, ext_dir) == before


def test_rm_yes_skips_prompt(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert "Stopped tracking 1 item; deleted " in result.output
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0


def test_rm_records_a_run_and_the_removed_item(
    archive: Path, tmp_path: Path, ext_dir: Path
) -> None:
    suffixed = dataclasses.replace(services(tmp_path, []), run_suffix="3f9a")
    with bootstrap.override(suffixed):
        runner.invoke(app, ["rm", "#all-naleid", "--yes", "--root", str(archive)])
    query = f"SELECT command, run FROM run ORDER BY {VERSION_ORDER}"
    command, run = lake_rows(archive, ext_dir, query)[-1]
    assert (command, run.endswith("_3f9a")) == ("rm", True)
    query = f"SELECT status FROM tracked_item WHERE channel = '{ALL_NALEID}'"
    assert lake_rows(archive, ext_dir, query) == [("removed",)]


def test_rm_then_list_hides_item(archive: Path, tmp_path: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    result = invoke(tmp_path, "list", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "C0CA5396G03" not in result.output
    assert "all-naleid" not in result.output
    assert "C0CA7SNHTSP" in result.output or "slarc-fixtures" in result.output


def test_rm_then_export_workspace_skips_removed(archive: Path, tmp_path: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    result = invoke(tmp_path, "export", "naleidworkspace", "--root", str(archive))
    assert result.exit_code == 0, result.output
    out = tmp_path / "work" / "slarc-export-naleidworkspace"
    assert not (out / "all-naleid.html").exists()
    assert (out / "slarc-fixtures.html").is_file()


def test_rm_unknown_selector_exits_2(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#no-such-channel", "--yes", "--root", str(archive))
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert snapshot(archive, ext_dir) == before


def test_rm_unparseable_selector_exits_2(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "rm", "not a link", "--root", str(archive))
    assert result.exit_code == 2
    assert "Cannot read 'not a link' as a selector" in result.output


def test_rm_again_cleans_up_nothing_to_do(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", CHANNEL_LINK, "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Nothing to remove."
    assert snapshot(archive, ext_dir) == before


def test_rm_cleanup_only_prompts_and_deletes(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    store = DuckLakeArchiveStore(archive, ext_dir, FakeArchiveLock())
    for line in recorded_lines(f"raw/{RECORDED_TEAM}/{ALL_NALEID}/*.jsonl"):
        store.append(envelope(line))
    later = "2026-10-20T00:00:00Z"
    store.commit(Commit())
    store.append_run(RunRecord(run="r_leftover", command="pull", started=later), "0.1.0")
    store.close()
    assert channel_messages(archive, ext_dir, ALL_NALEID) > 0
    result = invoke(tmp_path, "rm", CHANNEL_LINK, "--root", str(archive), input="y\n")
    assert result.exit_code == 0, result.output
    assert "Delete " in result.output
    assert "Stop tracking" not in result.output
    assert "Messages deleted from Slack" in result.output
    assert result.output.strip().splitlines()[-1].startswith("Cleaned up ")
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0


def test_rm_reports_a_maintenance_failure_after_committing(
    archive: Path, tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(*args: object) -> bool:
        raise MaintenanceFailed("No space left on device")

    monkeypatch.setattr(DuckLakeArchiveStore, "maintain", disk_full)
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 2
    output = " ".join(result.output.split())
    assert output == (
        "The run is saved, but maintaining the archive failed (No space left on device); "
        "the next pull or rm tries again."
    )
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0


def test_rm_help_no_longer_warns_about_a_running_pull(tmp_path: Path) -> None:
    result = invoke(tmp_path, "rm", "--help")
    assert result.exit_code == 0
    assert "pull is running" not in " ".join(result.output.split())


def message_rows_on_disk(root: Path, channel: str) -> int:
    """The channel's message rows in every Parquet file under data/, referenced or not."""
    con = duckdb.connect()
    try:
        count = 0
        for path in (root / DATA_DIR).rglob("*.parquet"):
            columns = {
                row[0]
                for row in con.execute("DESCRIBE FROM read_parquet(?)", [str(path)]).fetchall()
            }
            if {"channel", "kind"} <= columns:
                count += con.execute(
                    "SELECT count(*) FROM read_parquet(?) "
                    "WHERE channel = ? AND kind IN ('message', 'deleted')",
                    [str(path), channel],
                ).fetchall()[0][0]
        return count
    finally:
        con.close()


def catalog_tables_mentioning(root: Path, text: str) -> set[str]:
    con = sqlite3.connect(root / CATALOG)
    try:
        names = [
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            name
            for name in names
            if any(text in str(row) for row in con.execute(f'SELECT * FROM "{name}"'))
        }
    finally:
        con.close()


def blob_ids(root: Path) -> set[str]:
    return {path.name for path in (root / "blobs").rglob("*") if path.is_file()}


def referenced_blobs(root: Path, ext_dir: Path) -> set[str]:
    query = "SELECT DISTINCT blob FROM directory_entry WHERE blob IS NOT NULL"
    return {blob for (blob,) in lake_rows(root, ext_dir, query)}


def test_rm_physically_removes_a_conversation_when_idle(
    archive: Path, tmp_path: Path, ext_dir: Path
) -> None:
    before = blob_ids(archive)
    assert message_rows_on_disk(archive, ALL_NALEID) > 0
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("Stopped tracking 1 item; deleted ")

    assert message_rows_on_disk(archive, ALL_NALEID) == 0
    # The removed tracked item and the run's args keep the id, so their columns' minimum and
    # maximum can name it; no other catalog row, inlined rows included, does.
    assert catalog_tables_mentioning(archive, ALL_NALEID) <= {
        "ducklake_file_column_stats",
        "ducklake_table_column_stats",
    }
    after = blob_ids(archive)
    assert after < before
    assert after == referenced_blobs(archive, ext_dir)


def test_deletion_waits_while_another_process_holds_the_archive(
    archive: Path, tmp_path: Path, ext_dir: Path
) -> None:
    before = blob_ids(archive)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, archive, bootstrap._cache_home()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdin is not None
    assert holder.stdout.readline() == "held\n"
    try:
        result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert result.exit_code == 0, result.output
    assert " ".join(result.output.split()) == (
        "Stopped tracking 1 item. Their 14 messages, 4 files and 6 images are no longer in the "
        "archive; the space is freed when the running slarc command finishes."
    )
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0
    assert message_rows_on_disk(archive, ALL_NALEID) > 0
    assert blob_ids(archive) == before

    # Without a session the pull reaches no workspace, and its run still maintains the archive.
    invoke(tmp_path, "pull", "--root", str(archive))
    assert message_rows_on_disk(archive, ALL_NALEID) == 0
    assert blob_ids(archive) == referenced_blobs(archive, ext_dir) < before


def test_rm_thread_in_tracked_conversation_reports_kept(f: Fixture) -> None:  # noqa: F811
    assert f.pull("--yes", BASE).exit_code == 0
    assert f.pull(THREAD_LINK).exit_code == 0
    with bootstrap.override(f.services()):
        result = runner.invoke(app, ["rm", THREAD_LINK, "--yes"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("Stopped tracking 1 item.")
    assert " Kept " in result.output
    assert " message" in result.output and "other tracked items cover." in result.output


def test_rm_after_prompt_includes_messages_committed_meanwhile(
    archive: Path, tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores: list[ArchiveStore] = []
    questions: list[str] = []
    plan_removal = cli.plan_removal

    def plan_and_keep_store(*args, **kwargs) -> Removal:
        stores.append(args[2])
        return plan_removal(*args, **kwargs)

    def pull_commits_then_yes(question: str, default: bool) -> bool:
        """Commits a new message for the conversation, as a pull would, then confirms."""
        questions.append(question)
        [store] = stores
        [line, *_] = recorded_lines(f"raw/{RECORDED_TEAM}/{ALL_NALEID}/*.jsonl")
        late_ts = "1792500000.000100"
        line["meta"] |= {"ts": late_ts, "fetched_at": "2026-10-20T00:00:00Z", "run": "r_late"}
        line["object"] |= {"ts": late_ts}
        store.append(envelope(line))
        item = store.tracked_items()[(RECORDED_TEAM, "conversation", ALL_NALEID, "")]
        store.commit(Commit(items=[item]))
        return True

    monkeypatch.setattr(cli, "plan_removal", plan_and_keep_store)
    monkeypatch.setattr(cli.typer, "confirm", pull_commits_then_yes)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive))

    assert result.exit_code == 0, result.output
    assert channel_messages(archive, ext_dir, ALL_NALEID) == 0
    [question] = questions
    planned = re.search(r"delete (\d+) lines", question)
    printed = re.search(r"deleted (\d+) lines", result.output)
    assert planned is not None and printed is not None
    assert int(printed[1]) == int(planned[1]) + 1


def test_rm_that_keeps_conflicting_fails_and_removes_nothing(
    archive: Path, tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def conflict(store: DuckLakeArchiveStore, change: Commit) -> None:
        store.discard()
        raise CommitConflict()

    before = snapshot(archive, ext_dir)
    monkeypatch.setattr(DuckLakeArchiveStore, "commit", conflict)
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))

    assert result.exit_code == 2
    output = " ".join(result.output.split())
    assert output == "Other slarc commands kept changing these items; run rm again."
    after = snapshot(archive, ext_dir)
    for state in (before, after):
        assert isinstance(state["tables"], dict)
        del state["tables"]["run"]
    assert after == before


def test_cleanup_result_says_when_the_space_is_freed() -> None:
    removal = Removal(
        items=[],
        already_removed=[],
        teams=[RECORDED_TEAM],
        wiped_teams=[],
        lines=2,
        files=0,
        images=1,
        blobs=0,
        kept_messages=0,
        downloads=[],
    )
    assert cli._removal_result(removal, True) == "Cleaned up 2 lines and 1 image."
    assert cli._removal_result(removal, False) == (
        "Cleaned up 2 lines and 1 image; "
        "the space is freed when the running slarc command finishes."
    )
