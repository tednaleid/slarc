# ABOUTME: Tests for the `slarc list` and `slarc search` commands over a copy of the recorded data.
# ABOUTME: Credentials and Slack are fakes swapped in through bootstrap; only --remote uses them.
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.cli import app
from slarc.ports import Session
from tests.fakes import FakeArchiveLock, FakeCredentialSource, FakeKeyring, FakeSlackGateway
from tests.recorded_fixtures import recorded_lake
from tests.service.pull_harness import NALEID, spike
from tests.service.test_listing import AVATAR, GIPHY, leave_downloads_outstanding

runner = CliRunner()
SEED_LINK = "https://naleidworkspace.slack.com/archives/C0CA7SNHTSP/p1792437061952039"
ALL_NALEID_LINK = "https://naleidworkspace.slack.com/archives/C0CA5396G03"


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = recorded_lake(tmp_path / "archive")
    (root / "sub").mkdir()
    monkeypatch.chdir(root / "sub")
    monkeypatch.setattr(cli.out, "width", 200)
    return root


def services(home: Path, sessions: list[Session], gateway: FakeSlackGateway | None = None):
    def connect(_: Session, __: Callable[[int], None]) -> FakeSlackGateway:
        if gateway is None:
            pytest.fail("this command never connects to Slack")
        return gateway

    return bootstrap.Services(
        credentials=CompositeSource([FakeCredentialSource(sessions)]),
        keychain=KeyringSource(FakeKeyring()),
        gateway_factory=connect,
        env={},
        home=home,
    )


def invoke(home: Path, *args: str, sessions: list[Session] | None = None, gateway=None):
    with bootstrap.override(services(home, sessions or [], gateway)):
        return runner.invoke(app, list(args))


def test_cli_search_prints_hits(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "search", "SLARC SEED V1")
    assert result.exit_code == 0, result.output
    when = datetime.fromtimestamp(1792437061).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = result.output.splitlines()
    assert lines[0] == f"Ted Naleid in #slarc-fixtures, {when}"
    assert "slarc seed v1" in lines[1]
    assert lines[2].strip() == SEED_LINK


def test_cli_search_format_jsonl_keys(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "search", "SLARC SEED V1", "--format", "jsonl")
    assert result.exit_code == 0, result.output
    [hit] = [json.loads(line) for line in result.stdout.splitlines()]
    assert list(hit) == [
        "team_id",
        "workspace",
        "channel_id",
        "conversation",
        "ts",
        "thread_ts",
        "posted_at",
        "author",
        "text",
        "link",
        "deleted_at",
        "first_seen_at",
        "snippet",
    ]
    assert hit["link"] == SEED_LINK
    assert datetime.fromisoformat(hit["posted_at"]).timestamp() == 1792437061.952039


def test_cli_search_limit_hint_on_stderr(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "search", "--in", "#all-naleid", "--limit", "2")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("/archives/") == 2
    assert result.stderr.strip() == "Showing 2; --limit for more."


def test_cli_search_takes_a_window(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "search", "--since", "2000-01-01", "--until", "2000-01-02")
    assert result.exit_code == 1
    assert result.stderr.strip() == "No messages match."
    bad = invoke(tmp_path, "search", "seed", "--since", "last week")
    assert bad.exit_code == 2
    assert bad.stderr.startswith("--since must be a date")


def test_cli_search_filters_and_reports_no_matches(archive: Path, tmp_path: Path) -> None:
    result = invoke(
        tmp_path, "search", "joined the channel", "--in", "#all-naleid", "--from", "jim"
    )
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].startswith("Jim in #all-naleid, ")
    assert result.output.count("/archives/") == 1
    none = invoke(tmp_path, "search", "no such words anywhere")
    assert none.exit_code == 1, none.output
    assert none.stdout == ""
    assert none.stderr.strip() == "No messages match."
    unknown = invoke(tmp_path, "search", "seed", "--in", "#nope")
    assert unknown.exit_code == 2, unknown.output
    assert unknown.stdout == ""
    assert unknown.stderr.strip() == "No conversation named #nope in the archive. See slarc list."


def test_cli_list_format_jsonl_emits_one_item_per_line(archive: Path, tmp_path: Path) -> None:
    table = invoke(tmp_path, "list")
    result = invoke(tmp_path, "list", "--format", "jsonl")
    assert result.exit_code == 0, result.output
    items = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(items) == table.stdout.count("naleidworkspace")
    assert list(items[0]) == [
        "workspace",
        "conversation",
        "kind",
        "messages",
        "last_pull",
        "pending",
        "skipped",
        "status",
    ]
    assert "Archive:" not in result.stdout
    assert "Downloads:" not in result.stdout


def test_cli_list_prints_archive_config_and_items(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "list")
    assert result.exit_code == 0, result.output
    assert f"Archive: {archive}" in result.output
    assert f"Config: {archive / 'slarc.toml'}" in result.output
    assert (
        "Settings: downloads = 4, max_file_size_mb = 100, active_thread_days = 7, tz = local"
        in result.output
    )
    for text in ("#all-naleid", "@Jim", "14", "naleidworkspace", "Downloads: 0 pending, 0 skipped"):
        assert text in result.output


def test_cli_list_prints_the_settings_slarc_toml_gives(archive: Path, tmp_path: Path) -> None:
    (archive / "slarc.toml").write_text(
        '[pull]\nmax_file_size_mb = 200\n[export]\ntz = "America/Chicago"\n', encoding="utf-8"
    )
    result = invoke(tmp_path, "list")
    assert result.exit_code == 0, result.output
    assert (
        "Settings: downloads = 4, max_file_size_mb = 200, active_thread_days = 7, "
        "tz = America/Chicago" in result.output
    )


def test_cli_list_remote_marks_tracked(archive: Path, tmp_path: Path) -> None:
    infos = {c["id"]: c for c in spike("conversations.list")["channels"]}
    gateway = FakeSlackGateway({"C0CA5396G03": [], "C0CA53DLVJP": []}, infos=infos)
    result = invoke(tmp_path, "list", "--remote", sessions=[NALEID], gateway=gateway)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("#all-naleid" in line and "tracked" in line for line in lines)
    assert any("#new-channel" in line and "tracked" not in line for line in lines)


def test_cli_list_remote_without_session_fails(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "list", "--remote")
    assert result.exit_code == 2
    assert "No Slack session found for workspace" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("args", [["list"], ["list", "--remote"], ["search", "seed"]])
def test_cli_list_and_search_without_archive_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke(tmp_path, *args)
    assert result.exit_code == 2
    assert "No slarc archive found" in result.output
    assert "Traceback" not in result.output


def test_cli_search_and_sql_in_an_empty_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    (root / "slarc.toml").write_text("")
    monkeypatch.chdir(root)

    searched = invoke(tmp_path, "search", "seed")
    queried = invoke(tmp_path, "sql", "SELECT count(*) AS n FROM message")

    assert searched.exit_code == 1, searched.output
    assert searched.stderr.strip() == "No messages match."
    assert queried.exit_code == 0, queried.output
    assert "0" in queried.output


def test_cli_list_of_a_link_shows_each_download_on_one_line_with_failures_last(
    archive: Path, tmp_path: Path, ext_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.out, "width", 60)  # narrower than the URLs, which must not wrap
    store = DuckLakeArchiveStore(archive, ext_dir, FakeArchiveLock())
    try:
        leave_downloads_outstanding(store)
    finally:
        store.close()

    result = invoke(tmp_path, "list", ALL_NALEID_LINK)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "#slarc-fixtures" not in result.output
    header = next(line for line in lines if line.rstrip().endswith("URL"))
    assert header.split() == [
        "Workspace",
        "Conversation",
        "Type",
        "Status",
        "Attempts",
        "Error",
        "URL",
    ]
    giphy = next(line for line in lines if line.endswith(GIPHY))
    assert giphy.split()[:5] == ["naleidworkspace", "#all-naleid", "image", "forbidden", "1"]
    assert "Slack returned HTTP 403 for the image." in giphy
    avatar = next(line for line in lines if line.endswith(AVATAR))
    assert avatar.split()[:5] == ["naleidworkspace", "#all-naleid", "avatar", "failed", "2"]
    saved = [line for line in lines if " saved " in line]
    assert saved
    assert lines.index(saved[-1]) < lines.index(giphy) < lines.index(avatar)
    assert lines[-1] == "Downloads: 1 pending, 1 skipped."


def test_cli_list_remote_takes_no_links(archive: Path, tmp_path: Path) -> None:
    result = invoke(tmp_path, "list", "--remote", ALL_NALEID_LINK)

    assert result.exit_code == 2
    assert (
        result.output.strip() == "slarc list --remote lists every conversation; it takes no links."
    )
