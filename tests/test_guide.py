# ABOUTME: Tests for slarc guide: it prints the packaged guide, help points to it, and every
# ABOUTME: read command in the guide runs against the recorded archive with placeholders mapped.
import json
import re
import shlex
import shutil
from importlib.resources import files
from pathlib import Path

import pytest

from slarc import cli
from tests.recorded_fixtures import RECORDED, recorded_lake
from tests.test_cli_list_search import invoke

GUIDE = files("slarc").joinpath("data/guide.md").read_text(encoding="utf-8")
SH_BLOCK = re.compile(r"```sh\n(.*?)```", re.DOTALL)
# The guide's placeholder names, mapped to ones the recorded archive holds.
FIXTURE_NAMES = {
    "#general": "#all-naleid",
    "@ada": "@jim",
    "ada": "jim",
    "F0123456789": "F0CA7TKM285",
    "~/Downloads": ".",
    "acme": "naleidworkspace",
    "https://acme.slack.com/archives/C0123456789/p1712345678000100": (
        "https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792427679271249"
    ),
}


def guide_commands() -> list[list[str]]:
    """Each slarc command in the guide's sh blocks that only reads, as its arguments."""
    commands = []
    for block in SH_BLOCK.findall(GUIDE):
        for line in block.splitlines():
            args = shlex.split(line)
            if args[:1] == ["slarc"] and args[1] not in ("pull", "rm"):
                commands.append([FIXTURE_NAMES.get(arg, arg) for arg in args[1:]])
    return commands


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = recorded_lake(tmp_path / "archive")
    shutil.copytree(RECORDED / "blobs", root / "blobs")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def test_guide_prints_the_packaged_guide(tmp_path: Path) -> None:
    result = invoke(tmp_path, "guide")
    assert result.exit_code == 0, result.output
    assert result.stdout == GUIDE


def test_help_points_to_the_guide(tmp_path: Path) -> None:
    result = invoke(tmp_path, "--help")
    assert "slarc guide" in result.stdout


def test_guide_examples_run_against_the_recorded_archive(archive: Path, tmp_path: Path) -> None:
    commands = guide_commands()
    assert len(commands) >= 10
    for args in commands:
        result = invoke(tmp_path, *args, "--root", str(archive))
        assert result.exit_code in (0, 1), (args, result.output)
        if "jsonl" in args:
            for line in result.stdout.splitlines():
                json.loads(line)


def test_the_first_40_lines_are_enough_to_search_query_and_read_a_file() -> None:
    head = "\n".join(GUIDE.splitlines()[:40])
    for fact in ("DuckDB", "not SQLite", "message_readable", "author_real_name", "posted_at"):
        assert fact in head, fact
    assert "slarc rm" in head
    commands = [line for block in SH_BLOCK.findall(head + "\n```") for line in block.splitlines()]
    assert [line.split()[1] for line in commands] == ["search", "sql", "file"]
    assert "FROM message_readable" in commands[1]
