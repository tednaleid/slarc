# ABOUTME: Tests for the `slarc export` command over a lake of the recorded archive with its blobs.
# ABOUTME: The command writes real files under a temp directory and never connects to Slack.
import dataclasses
import io
import shutil
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.cli import app
from tests.recorded_fixtures import RECORDED, recorded_lake
from tests.test_cli_list_search import services

runner = CliRunner()
SCREENSHOT = "ca3f1d09a38b3f0dc102411b3c732a8cabc6cdb275e88457417ae32d098578fd"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = recorded_lake(tmp_path / "archive")
    shutil.copytree(RECORDED / "blobs", root / "blobs")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def invoke(home: Path, *args: str, opened: list[str] | None = None):
    def open_page(uri: str) -> bool:
        if opened is None:
            pytest.fail("export opened a browser without --open")
        opened.append(uri)
        return True

    with bootstrap.override(dataclasses.replace(services(home, []), open_page=open_page)):
        return runner.invoke(app, list(args))


def test_cli_export_writes_default_directory(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "export", "#all-naleid", "--root", str(archive))
    assert result.exit_code == 0, result.output
    out = tmp_path / "work" / "slarc-export-all-naleid"
    assert result.output.splitlines() == [
        "Exported 1 workspace, 1 conversation, 2 threads, 14 messages, 4 files and 6 reactions to",
        str(out / "index.html"),
    ]
    assert (out / "index.html").is_file()
    assert "Hello!" in (out / "C0CA5396G03.html").read_text(encoding="utf-8")
    copied = out / "assets" / f"{SCREENSHOT}.png"
    assert copied.read_bytes() == (archive / "blobs/sha256/ca/3f" / SCREENSHOT).read_bytes()


def test_cli_export_says_how_many_files_it_does_not_hold(archive: Path, tmp_path: Path) -> None:
    (archive / "blobs/sha256/ca/3f" / SCREENSHOT).unlink()
    result = invoke(tmp_path, "export", "#all-naleid", "--root", str(archive))
    assert result.exit_code == 0, result.output
    assert result.stderr.strip() == (
        "1 file is not in the export; run slarc list '#all-naleid' to see why."
    )
    assert "not in the export" not in result.stdout


def test_cli_export_workspace_writes_default_directory(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "export", "naleidworkspace", "--root", str(archive))
    assert result.exit_code == 0, result.output
    out = tmp_path / "work" / "slarc-export-naleidworkspace"
    assert (out / "index.html").is_file()
    assert (out / "C0CA5396G03.html").is_file()


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


def test_cli_export_links_the_page_to_open_in_a_terminal(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = io.StringIO()
    monkeypatch.setattr(cli, "out", Console(file=terminal, force_terminal=True, width=200))

    result = invoke(tmp_path, "export", "#all-naleid", "--root", str(archive))

    assert result.exit_code == 0, result.output
    index = tmp_path / "work" / "slarc-export-all-naleid" / "index.html"
    assert "\x1b]8;id=" in terminal.getvalue()
    assert f";{index.as_uri()}\x1b\\{index}\x1b]8;;\x1b\\" in terminal.getvalue()


def test_cli_export_open_opens_the_index_in_the_browser(archive: Path, tmp_path: Path) -> None:
    opened: list[str] = []

    result = invoke(
        tmp_path, "export", "#all-naleid", "--open", "--root", str(archive), opened=opened
    )

    assert result.exit_code == 0, result.output
    index = tmp_path / "work" / "slarc-export-all-naleid" / "index.html"
    assert opened == [index.as_uri()]


def test_cli_export_open_without_a_browser_says_to_use_the_link(
    archive: Path, tmp_path: Path
) -> None:
    services_ = dataclasses.replace(services(tmp_path, []), open_page=lambda _: False)
    with bootstrap.override(services_):
        result = runner.invoke(app, ["export", "#all-naleid", "--open", "--root", str(archive)])

    assert result.exit_code == 0, result.output
    assert result.stderr.strip() == "No browser could be opened; open the page above instead."


def test_cli_export_open_opens_a_single_file(archive: Path, tmp_path: Path) -> None:
    opened: list[str] = []
    target = tmp_path / "one.html"

    result = invoke(
        tmp_path,
        "export",
        "#all-naleid",
        "--single-file",
        "--out",
        str(target),
        "--open",
        "--root",
        str(archive),
        opened=opened,
    )

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == str(target)
    assert opened == [target.as_uri()]


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
    assert result.exit_code == 2
    assert message in result.output
    assert "Traceback" not in result.output


def test_cli_export_without_archive_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke(tmp_path, "export", "#all-naleid")
    assert result.exit_code == 2
    assert "No slarc archive found" in result.output


def test_cli_export_to_stdout_keeps_notes_on_stderr(archive: Path, tmp_path: Path) -> None:
    result = invoke(
        tmp_path,
        "export",
        "#all-naleid",
        "--format",
        "markdown",
        "--out",
        "-",
        "--root",
        str(archive),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("# #all-naleid (naleidworkspace)\n")
    assert "Exported" not in result.stdout
    assert result.stderr.startswith("Exported 1 workspace, 1 conversation")
    assert list((tmp_path / "work").iterdir()) == []


def test_cli_export_markdown_directory_has_one_md_per_page(archive: Path, tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "export", "naleidworkspace", "--format", "markdown", "--root", str(archive)
    )
    assert result.exit_code == 0, result.output
    out = tmp_path / "work" / "slarc-export-naleidworkspace"
    names = {path.name for path in out.iterdir()}
    assert "C0CA5396G03.md" in names
    assert all(name.endswith(".md") for name in names)
    assert result.stdout.splitlines()[-1] == str(out)


def test_cli_export_window_and_channel(archive: Path, tmp_path: Path) -> None:
    link = "https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792427577619569"
    result = invoke(
        tmp_path,
        "export",
        link,
        "--channel",
        "--since",
        "2000-01-01",
        "--format",
        "markdown",
        "--out",
        "-",
        "--root",
        str(archive),
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.count("(linked)") == 1
    empty = invoke(
        tmp_path, "export", "#all-naleid", "--until", "2000-01-01", "--root", str(archive)
    )
    assert empty.exit_code == 2
    assert "fall in the window" in empty.stderr
