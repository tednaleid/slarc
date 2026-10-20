# ABOUTME: Tests for the `slarc rm` command over a copy of the recorded archive, without Slack.
# ABOUTME: One test pulls with the pull fixture to track a thread inside a tracked conversation.
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.cli import app
from slarc.ports import ArchiveWriteFailed
from tests.recorded_fixtures import RECORDED, copy_recorded
from tests.test_cli_list_search import services
from tests.test_cli_pull import BASE, Fixture, f  # noqa: F401

runner = CliRunner()
CHANNEL_LINK = "https://naleidworkspace.slack.com/archives/C0CA5396G03"
THREAD_LINK = f"{BASE}/p1792410693663869"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = copy_recorded(tmp_path / "archive")
    shutil.copytree(RECORDED / "blobs", root / "blobs")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def invoke(home: Path, *args: str, input: str | None = None):
    with bootstrap.override(services(home, [])):
        return runner.invoke(app, list(args), input=input)


def snapshot(root: Path) -> dict[Path, bytes]:
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def line_count(root: Path) -> int:
    return sum(len(p.read_bytes().splitlines()) for p in root.glob("raw/**/*.jsonl"))


def test_rm_prompts_and_deletes_on_yes(archive: Path, tmp_path: Path) -> None:
    before = line_count(archive)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="y\n")
    assert result.exit_code == 0, result.output
    assert "Stop tracking 1 item and delete " in result.output
    assert "Messages deleted from Slack since they were pulled cannot be pulled again." in (
        result.output
    )
    assert "[y/N]" in result.output
    assert not (archive / "raw/T0CB5LYPVFA/C0CA5396G03").exists()
    assert line_count(archive) < before
    assert result.output.strip().splitlines()[-1].startswith("Stopped tracking 1 item; deleted ")


def test_rm_declined_changes_nothing(archive: Path, tmp_path: Path) -> None:
    before = snapshot(archive)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="n\n")
    assert result.exit_code == 1
    assert "Nothing removed." in result.output
    assert snapshot(archive) == before


def test_rm_default_answer_is_no(archive: Path, tmp_path: Path) -> None:
    before = snapshot(archive)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="\n")
    assert result.exit_code == 1
    assert snapshot(archive) == before


def test_rm_aborted_prompt_changes_nothing(archive: Path, tmp_path: Path) -> None:
    before = snapshot(archive)
    result = invoke(tmp_path, "rm", "#all-naleid", "--root", str(archive), input="")
    assert result.exit_code == 1
    assert "Nothing removed." in result.output
    assert "Aborted!" not in result.output
    assert snapshot(archive) == before


def test_rm_yes_skips_prompt(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert "Stopped tracking 1 item; deleted " in result.output
    assert not (archive / "raw/T0CB5LYPVFA/C0CA5396G03").exists()


def test_rm_records_a_run_line(archive: Path, tmp_path: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    last = (archive / "runs.jsonl").read_text().splitlines()[-1]
    assert '"command":"rm"' in last.replace(" ", "")
    assert '"status":"removed"' in last.replace(" ", "")


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


def test_rm_unknown_selector_exits_1(archive: Path, tmp_path: Path) -> None:
    before = snapshot(archive)
    result = invoke(tmp_path, "rm", "#no-such-channel", "--yes", "--root", str(archive))
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert snapshot(archive) == before


def test_rm_unparseable_selector_exits_1(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "rm", "not a link", "--root", str(archive))
    assert result.exit_code == 1
    assert "Cannot read 'not a link' as a selector" in result.output


def test_rm_again_cleans_up_nothing_to_do(archive: Path, tmp_path: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    before = snapshot(archive)
    result = invoke(tmp_path, "rm", CHANNEL_LINK, "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Nothing to remove."
    assert snapshot(archive) == before


def test_rm_cleanup_only_prompts_and_sweeps(archive: Path, tmp_path: Path) -> None:
    invoke(tmp_path, "rm", "#all-naleid", "--yes", "--root", str(archive))
    leftover = archive / "raw/T0CB5LYPVFA/C0CA5396G03"
    leftover.mkdir()
    source = RECORDED / "raw/T0CB5LYPVFA/C0CA5396G03/2026-10.jsonl"
    shutil.copy(source, leftover / "2026-10.jsonl")
    result = invoke(tmp_path, "rm", CHANNEL_LINK, "--root", str(archive), input="y\n")
    assert result.exit_code == 0, result.output
    assert "Delete " in result.output
    assert "Stop tracking" not in result.output
    assert "Messages deleted from Slack" in result.output
    assert result.output.strip().splitlines()[-1].startswith("Cleaned up ")
    assert not leftover.exists()


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
    month = archive / "raw/T0CB5LYPVFA/C0CA5396G03/2026-10.jsonl"

    def disk_full(*args: object) -> None:
        raise ArchiveWriteFailed(month, "No space left on device")

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
