# ABOUTME: Tests for the `slarc export` command over a copy of the recorded archive with its blobs.
# ABOUTME: The command writes real files under a temp directory and never connects to Slack.
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.cli import app
from tests.recorded_fixtures import RECORDED, copy_recorded
from tests.test_cli_list_search import services

runner = CliRunner()
SCREENSHOT = "ca3f1d09a38b3f0dc102411b3c732a8cabc6cdb275e88457417ae32d098578fd"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = copy_recorded(tmp_path / "archive")
    shutil.copytree(RECORDED / "blobs", root / "blobs")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def invoke(home: Path, *args: str):
    with bootstrap.override(services(home, [])):
        return runner.invoke(app, list(args))


def test_cli_export_writes_default_directory(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "export", "#all-naleid", "--root", str(archive))
    assert result.exit_code == 0, result.output
    out = tmp_path / "work" / "slarc-export-all-naleid"
    assert result.output.strip() == f"Exported to {out}."
    assert (out / "index.html").is_file()
    assert "Hello!" in (out / "C0CA5396G03.html").read_text(encoding="utf-8")
    copied = out / "assets" / f"{SCREENSHOT}.png"
    assert copied.read_bytes() == (archive / "blobs/sha256/ca/3f" / SCREENSHOT).read_bytes()


def test_cli_export_single_file_with_tz(archive: Path, tmp_path: Path) -> None:
    target = tmp_path / "one.html"
    result = invoke(
        tmp_path,
        "export",
        "https://naleidworkspace.slack.com/archives/C0CA5396G03",
        "--single-file",
        "--out",
        str(target),
        "--tz",
        "America/Chicago",
        "--root",
        str(archive),
    )
    assert result.exit_code == 0, result.output
    page = target.read_text(encoding="utf-8")
    assert 'data-tz="America/Chicago"' in page
    assert "data:image/png;base64," in page
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["#no-such-channel"], "No conversation named #no-such-channel"),
        (["#all-naleid", "--tz", "Mars/Olympus"], "Unknown time zone 'Mars/Olympus'"),
        (["not a link"], "Cannot read 'not a link' as a selector"),
    ],
)
def test_cli_export_errors_are_plain(
    archive: Path, tmp_path: Path, args: list[str], message: str
) -> None:
    result = invoke(tmp_path, "export", *args, "--root", str(archive))
    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output


def test_cli_export_without_archive_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke(tmp_path, "export", "#all-naleid")
    assert result.exit_code == 1
    assert "No slarc archive found" in result.output
