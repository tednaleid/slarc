# ABOUTME: Tests for the `slarc rm` command over a lake of the recorded archive, without Slack.
# ABOUTME: One test pulls with the pull fixture to track a thread inside a tracked conversation.
import dataclasses
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.adapters.lake import CATALOG
from slarc.adapters.lake_store import VERSION_ORDER, DuckLakeArchiveStore
from slarc.cli import app
from slarc.domain.tracking import RunRecord
from slarc.ports import ArchiveStore, ArchiveWriteFailed, Commit, CommitConflict
from slarc.service.remove import Removal
from tests.contract.test_duckdb_index import envelope
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
    assert result.exit_code == 1
    assert "Nothing removed." in result.output
    assert snapshot(archive, ext_dir) == before


def test_rm_default_answer_is_no(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="\n")
    assert result.exit_code == 1
    assert snapshot(archive, ext_dir) == before


def test_rm_aborted_prompt_changes_nothing(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="")
    assert result.exit_code == 1
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
    assert not (out / "C0CA5396G03.html").exists()
    assert (out / "C0CA7SNHTSP.html").is_file()


def test_rm_unknown_selector_exits_1(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", "#no-such-channel", "--yes", "--root", str(archive))
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert snapshot(archive, ext_dir) == before


def test_rm_unparseable_selector_exits_1(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "rm", "not a link", "--root", str(archive))
    assert result.exit_code == 1
    assert "Cannot read 'not a link' as a selector" in result.output


def test_rm_again_cleans_up_nothing_to_do(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    before = snapshot(archive, ext_dir)
    result = invoke(tmp_path, "rm", CHANNEL_LINK, "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Nothing to remove."
    assert snapshot(archive, ext_dir) == before


def test_rm_cleanup_only_prompts_and_sweeps(archive: Path, tmp_path: Path, ext_dir: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    store = DuckLakeArchiveStore(archive, ext_dir)
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


def test_rm_interrupted_sweep_says_to_run_it_again(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sweep", interrupt)
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 130
    output = " ".join(result.output.split())
    assert "Interrupted; progress so far is saved. Run the same slarc rm again to finish it." in (
        output
    )


def test_rm_failed_sweep_says_to_run_it_again(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(*args: object) -> None:
        raise ArchiveWriteFailed(archive / CATALOG, "No space left on device")

    monkeypatch.setattr(cli, "sweep", disk_full)
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "No space left on device. Run the same slarc rm again to finish it." in output


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

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert output == "Other slarc commands kept changing these items; run rm again."
    after = snapshot(archive, ext_dir)
    for state in (before, after):
        assert isinstance(state["tables"], dict)
        del state["tables"]["run"]
    assert after == before
