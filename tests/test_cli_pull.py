# ABOUTME: Tests for the `slarc pull` command through the CLI with a real archive on disk.
# ABOUTME: Slack, credentials, the clock and the downloader are fakes swapped in through bootstrap.
import io
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import msgspec
import pytest
from rich.console import Console
from typer.testing import CliRunner

from slarc import bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.adapters.lake import CATALOG, connect_lake
from slarc.adapters.lake_store import VERSION_ORDER
from slarc.cli import RichReporter, _summary, app
from slarc.config import create_archive
from slarc.domain.tracking import RunRecord
from slarc.ports import AuthFailed, BlobStore, CredentialReadError, Session
from slarc.service.downloads import QUEUED_STATUSES
from slarc.service.pull import PullReport
from tests.fakes import (
    FakeClock,
    FakeCredentialSource,
    FakeDownloader,
    FakeKeyring,
    FakeSlackGateway,
)
from tests.service.pull_harness import (
    BASE,
    CHANNEL,
    NALEID,
    OTHER,
    OTHER_LINK,
    OTHER_TEAM,
    TEAM,
    Harness,
    other_gateway,
)

runner = CliRunner()
PROMPT = "No archive here. Create one in"


def lake_rows(root: Path, ext_dir: Path, query: str) -> list[tuple]:
    """The rows of a query over the archive's lake tables."""
    con = connect_lake(root, ext_dir, read_only=True)
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def lake_count(root: Path, ext_dir: Path, table: str) -> int:
    return lake_rows(root, ext_dir, f"SELECT count(*) FROM {table}")[0][0]


class Fixture:
    def __init__(self, work: Path, home: Path, ext_dir: Path) -> None:
        self.work = work
        self.home = home
        self.ext_dir = ext_dir
        self.env: dict[str, str] = {}
        self.sessions: list[Session] = [NALEID]
        self.credential_error: CredentialReadError | None = None
        self.gateway = Harness().gateway
        self.gateways: dict[str, FakeSlackGateway] = {}
        self.clock = FakeClock()
        self.downloader = FakeDownloader()
        self.download_sessions: list[dict[str, Session]] = []

    def services(self) -> bootstrap.Services:
        def downloader(blobs: BlobStore, sessions: dict[str, Session]) -> nullcontext:
            self.download_sessions.append(sessions)
            return nullcontext(self.downloader)

        return bootstrap.Services(
            credentials=CompositeSource(
                [FakeCredentialSource(self.sessions, self.credential_error)]
            ),
            keychain=KeyringSource(FakeKeyring()),
            gateway_factory=lambda session: self.gateways.get(session.team_id, self.gateway),
            clock=self.clock,
            downloader=downloader,
            env=self.env,
            home=self.home,
        )

    def pull(self, *args: str, input: str | None = None):
        try:
            with bootstrap.override(self.services()):
                return runner.invoke(app, ["pull", *args], input=input)
        finally:
            self.clock.advance(3600)

    def runs(self) -> list[RunRecord]:
        query = f"SELECT object FROM run ORDER BY {VERSION_ORDER}"
        rows = lake_rows(self.work, self.ext_dir, query)
        return [msgspec.json.decode(obj, type=RunRecord) for (obj,) in rows]

    def messages(self, channel: str | None = None) -> int:
        if channel is None:
            return lake_count(self.work, self.ext_dir, "message_version")
        query = f"SELECT count(*) FROM message_version WHERE channel = '{channel}'"
        return lake_rows(self.work, self.ext_dir, query)[0][0]

    def directory_entries(self) -> int:
        return lake_count(self.work, self.ext_dir, "directory_entry")


@pytest.fixture
def f(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ext_dir: Path) -> Iterator[Fixture]:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    yield Fixture(work, tmp_path / "home", ext_dir)


def test_pull_creates_archive_after_confirmation(f: Fixture):
    result = f.pull(BASE, input="y\n")
    assert result.exit_code == 0, result.output
    assert f"{PROMPT} {f.work}? [Y/n]" in result.output
    assert (f.work / "slarc.toml").is_file()
    assert f.messages(CHANNEL) > 0


def test_new_archive_creates_a_lake(f: Fixture):
    result = f.pull("--yes", BASE)
    assert result.exit_code == 0, result.output
    assert (f.work / "catalog.sqlite").is_file()
    assert "Imported" not in result.output


def test_pull_declined_creates_nothing(f: Fixture):
    result = f.pull(BASE, input="n\n")
    assert result.exit_code == 1
    assert "No archive created." in result.output
    assert list(f.work.iterdir()) == []
    assert f.gateway.calls == []


def test_bare_pull_without_an_archive_says_nothing_is_tracked(f: Fixture):
    result = f.pull("--yes")
    assert result.exit_code == 0, result.output
    assert "No tracked items yet. Pull a Slack link to start tracking it." in result.output
    assert list(f.work.iterdir()) == []
    assert f.gateway.calls == []


def test_workspace_only_pull_without_an_archive_says_nothing_is_tracked(f: Fixture):
    result = f.pull("--yes", "acme")
    assert result.exit_code == 0, result.output
    assert "No tracked items yet. Pull a Slack link to start tracking it." in result.output
    assert list(f.work.iterdir()) == []
    assert f.gateway.calls == []


def test_bare_pull_with_nothing_tracked_says_so(f: Fixture):
    create_archive(f.work)
    result = f.pull()
    assert result.exit_code == 0, result.output
    assert "No tracked items yet. Pull a Slack link to start tracking it." in result.output
    assert "Pulled" not in result.output


def test_create_prompt_aborted_creates_nothing(f: Fixture):
    result = f.pull(BASE, input="")
    assert result.exit_code == 1
    assert "No archive created." in result.output
    assert "Aborted!" not in result.output
    assert list(f.work.iterdir()) == []


def test_pull_yes_skips_prompt(f: Fixture):
    result = f.pull("--yes", BASE)
    assert result.exit_code == 0, result.output
    assert PROMPT not in result.output
    assert (f.work / "slarc.toml").is_file()


def test_pull_uses_existing_archive_in_a_parent_directory(f: Fixture, monkeypatch):
    (f.work / "slarc.toml").write_text("")
    nested = f.work / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    result = f.pull(BASE)
    assert result.exit_code == 0, result.output
    assert PROMPT not in result.output
    assert len(f.runs()) == 1
    assert not (nested / CATALOG).exists()


def test_pull_root_without_slarc_toml_creates_archive_after_confirmation(f: Fixture, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    result = f.pull("--root", str(other), BASE, input="y\n")
    assert result.exit_code == 0, result.output
    assert f"No archive at {other}. Create one? [Y/n]" in result.output
    assert (other / "slarc.toml").is_file()
    assert lake_count(other, f.ext_dir, "run") == 1
    assert list(f.work.iterdir()) == []


def test_pull_env_archive_without_slarc_toml_declined_writes_nothing(f: Fixture, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    f.env["SLARC_ARCHIVE"] = str(other)
    result = f.pull(BASE, input="n\n")
    assert result.exit_code == 1
    assert f"No archive at {other}. Create one? [Y/n]" in result.output
    assert "No archive created." in result.output
    assert list(other.iterdir()) == []
    assert f.gateway.calls == []


def test_pull_yes_creates_archive_at_missing_root(f: Fixture, tmp_path):
    other = tmp_path / "missing"
    result = f.pull("--yes", "--root", str(other), BASE)
    assert result.exit_code == 0, result.output
    assert "Create one?" not in result.output
    assert (other / "slarc.toml").is_file()


def test_pull_root_with_archive_skips_prompt(f: Fixture, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "slarc.toml").write_text("")
    result = f.pull("--root", str(other), BASE)
    assert result.exit_code == 0, result.output
    assert "Create one" not in result.output
    assert lake_count(other, f.ext_dir, "run") == 1


def test_pull_without_selectors_refreshes_tracked(f: Fixture):
    assert f.pull("--yes", BASE).exit_code == 0
    history_reads = sum(1 for call in f.gateway.calls if call[0] == "conversations.history")

    result = f.pull()
    assert result.exit_code == 0, result.output
    later_reads = sum(1 for call in f.gateway.calls if call[0] == "conversations.history")
    assert later_reads > history_reads
    first, second = f.runs()
    assert second.args == []
    assert [(item.kind, item.channel, item.status) for item in second.tracked] == [
        ("conversation", CHANNEL, "ok")
    ]
    assert second.tracked[0].last_pull_at != first.tracked[0].last_pull_at


def test_pull_reports_summary(f: Fixture):
    result = f.pull("--yes", BASE)
    assert result.exit_code == 0, result.output
    downloads = f.runs()[-1].downloads
    pending = sum(1 for state in downloads if state.status in QUEUED_STATUSES)
    assert pending > 0
    messages = f.messages()
    directory = f.directory_entries()
    assert result.stdout.splitlines() == [
        f"Pulled 1 item: {messages} new messages, {directory} new directory lines "
        "(users, emoji, files).",
        f"Downloads: {pending} pending for the next pull.",
    ]


def report(**counts: int) -> PullReport:
    fields = {
        "items_pulled": 1,
        "lines_appended": 0,
        "messages_new": 0,
        "files_saved": 0,
        "images_saved": 0,
        "downloads_pending": 0,
        "downloads_skipped": 0,
        "not_visible": 0,
    }
    fields.update(counts)
    return PullReport(
        items_selected=1,
        messages_seen=0,
        skipped=[],
        unreachable=[],
        workspaces_pulled=1,
        **fields,
    )


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (
            {"lines_appended": 3, "messages_new": 3},
            ["Pulled 1 item: 3 new messages, 0 new directory lines."],
        ),
        (
            {"items_pulled": 2, "lines_appended": 2, "messages_new": 1},
            ["Pulled 2 items: 1 new message, 1 new directory line (users, emoji, files)."],
        ),
        (
            {
                "lines_appended": 2470,
                "messages_new": 608,
                "files_saved": 85,
                "images_saved": 74,
                "downloads_pending": 7,
                "downloads_skipped": 8,
            },
            [
                "Pulled 1 item: 608 new messages, 1862 new directory lines (users, emoji, files).",
                "Downloads: 85 files and 74 images saved, 7 pending for the next pull, 8 skipped.",
            ],
        ),
        (
            {"files_saved": 1, "downloads_skipped": 1},
            [
                "Pulled 1 item: 0 new messages, 0 new directory lines.",
                "Downloads: 1 file saved, 1 skipped.",
            ],
        ),
        (
            {"not_visible": 1},
            [
                "Pulled 1 item: 0 new messages, 0 new directory lines.",
                "1 item not visible to this account; see slarc list.",
            ],
        ),
        (
            {"not_visible": 2},
            [
                "Pulled 1 item: 0 new messages, 0 new directory lines.",
                "2 items not visible to this account; see slarc list.",
            ],
        ),
        (
            {"images_saved": 1},
            [
                "Pulled 1 item: 0 new messages, 0 new directory lines.",
                "Downloads: 1 image saved.",
            ],
        ),
    ],
)
def test_summary_names_messages_directory_lines_and_downloads(
    counts: dict[str, int], expected: list[str]
):
    assert _summary(report(**counts)).splitlines() == expected


@pytest.mark.parametrize(
    ("failed", "skipped", "detail"),
    [
        (0, 0, "7/7"),
        (1, 0, "7/7, 1 failed"),
        (0, 2, "7/7, 2 skipped"),
        (1, 2, "7/7, 1 failed, 2 skipped"),
    ],
)
def test_reporter_shows_item_and_download_rows(failed: int, skipped: int, detail: str):
    buffer = io.StringIO()
    console = Console(file=buffer, width=120)

    with RichReporter(console) as reporter:
        reporter.downloads(0, 0, 0, 7)
        reporter.progress("#all-naleid", 3, 2, 3)
        reporter.downloads(7, failed, skipped, 7)

    downloads, item = buffer.getvalue().splitlines()
    assert item.startswith("#all-naleid")
    assert item.rstrip().endswith("3 read, 2 new")
    assert downloads.startswith("Downloads")
    assert downloads.rstrip().endswith(f" {detail}")


def test_slarc_error_exits_1_without_traceback(f: Fixture):
    f.gateway.auth_error = AuthFailed("desktop", "naleidworkspace")
    result = f.pull("--yes", BASE)
    assert result.exit_code == 1
    assert result.output.startswith("Slack rejected the desktop credentials")
    assert "Traceback" not in result.output
    assert len(f.runs()) == 1


def test_no_session_shows_credential_warnings(f: Fixture):
    error = CredentialReadError("desktop", "keychain read refused")
    f.sessions = []
    f.credential_error = error
    result = f.pull("--yes", BASE)
    assert result.exit_code == 1
    assert "keychain read refused" in result.output
    assert "No Slack session found for workspace naleidworkspace" in result.output


def test_unreadable_selector_exits_1(f: Fixture):
    result = f.pull("--yes", "not a link")
    assert result.exit_code == 1
    assert "Cannot read 'not a link' as a selector" in result.output
    assert list(f.work.iterdir()) == []


def test_range_without_selectors_exits_1(f: Fixture):
    result = f.pull("--yes", "--since", "2026-10-01")
    assert result.exit_code == 1
    assert "--since and --until apply only to links named on the command line." in result.output
    assert list(f.work.iterdir()) == []


def test_range_with_a_workspace_selector_exits_1(f: Fixture):
    result = f.pull("--yes", "naleidworkspace", "--since", "2026-10-01")
    assert result.exit_code == 1
    assert (
        "--since and --until apply to links and conversations, not to a whole workspace."
        in result.output
    )
    assert list(f.work.iterdir()) == []


def test_malformed_date_exits_1(f: Fixture):
    result = f.pull("--yes", "--until", "10/19/2026", BASE)
    assert result.exit_code == 1
    assert "--until must be a date like 2026-10-19." in result.output


def test_interrupt_exits_130_and_keeps_run_line(f: Fixture, monkeypatch: pytest.MonkeyPatch):
    def interrupt(*_args: object) -> list[dict]:
        raise KeyboardInterrupt

    monkeypatch.setattr(f.gateway, "replies", interrupt)
    result = f.pull("--yes", BASE)
    assert result.exit_code == 130
    assert "Interrupted; progress so far is saved." in result.output
    assert "Traceback" not in result.output
    assert len(f.runs()) == 1


def test_settings_reach_the_downloader(f: Fixture):
    (f.work / "slarc.toml").write_text("[pull]\ndownloads = 2\nmax_file_size_mb = 7\n")
    assert f.pull(BASE).exit_code == 0
    assert f.downloader.parallelism == [2]
    assert {job.max_bytes for job in f.downloader.jobs} == {7 * 1024 * 1024}


def test_flags_override_settings(f: Fixture):
    (f.work / "slarc.toml").write_text("[pull]\ndownloads = 2\n")
    assert f.pull("--downloads", "3", "--max-file-size", "5", BASE).exit_code == 0
    assert f.downloader.parallelism == [3]
    assert {job.max_bytes for job in f.downloader.jobs} == {5 * 1024 * 1024}


def test_downloader_gets_sessions_of_authenticated_workspaces(f: Fixture):
    env_session = Session("", "", "", "env", "xoxc-env", "xoxd-env")
    f.sessions = [env_session, NALEID]
    assert f.pull("--yes", BASE).exit_code == 0
    assert f.download_sessions == [{TEAM: env_session}]


def test_run_records_the_command_line(f: Fixture):
    assert f.pull("--yes", "--since", "2026-10-01", "--full", BASE).exit_code == 0
    run = f.runs()[-1]
    assert run.command == "pull"
    assert run.args == [BASE, "--since", "2026-10-01", "--full"]


def test_env_session_for_another_workspace_exits_1(f: Fixture):
    f.sessions = [Session("", "", "", "env", "xoxc-env", "xoxd-env")]
    result = f.pull("--yes", f"https://other.slack.com/archives/{CHANNEL}")
    assert result.exit_code == 1
    assert "The env credentials are for workspace naleidworkspace, not other." in result.output
    assert "Traceback" not in result.output
    assert f.runs()[-1].tracked == []


def test_bare_pull_skips_a_workspace_without_a_session(f: Fixture):
    f.sessions = [OTHER, NALEID]
    f.gateways[OTHER_TEAM] = other_gateway()
    assert f.pull("--yes", OTHER_LINK, BASE).exit_code == 0
    f.sessions = [NALEID]
    result = f.pull()
    assert result.exit_code == 0, result.output
    assert f"Could not connect to workspace {OTHER_TEAM}" in result.output
    assert "Pulled 1 item:" in result.stdout
    assert [(item.team_id, item.status) for item in f.runs()[-1].tracked] == [(TEAM, "ok")]


def test_bare_pull_exits_1_when_a_workspace_rejects_its_credentials(f: Fixture):
    f.sessions = [OTHER, NALEID]
    f.gateways[OTHER_TEAM] = other_gateway()
    assert f.pull("--yes", OTHER_LINK, BASE).exit_code == 0
    f.gateways[OTHER_TEAM].auth_error = AuthFailed("desktop", "other")
    result = f.pull()
    assert result.exit_code == 1
    assert "Slack rejected the desktop credentials for workspace other." in result.output
    assert "Pulled 1 item:" in result.stdout
    assert "Traceback" not in result.output


def test_bare_pull_exits_1_when_no_workspace_connects(f: Fixture):
    assert f.pull("--yes", BASE).exit_code == 0
    f.sessions = []
    f.credential_error = CredentialReadError("desktop", "keychain read refused")
    result = f.pull()
    assert result.exit_code == 1
    assert f"No Slack session found for workspace {TEAM}" in result.output
    assert "keychain read refused" in result.output
    assert "Traceback" not in result.output


def test_pull_into_a_read_only_archive_exits_1_without_traceback(f: Fixture):
    (f.work / "slarc.toml").write_text("")
    f.work.chmod(0o555)
    try:
        result = f.pull(BASE)
    finally:
        f.work.chmod(0o755)
    assert result.exit_code == 1
    assert "Could not write to" in result.output
    assert "Traceback" not in result.output
