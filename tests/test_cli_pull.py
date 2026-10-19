# ABOUTME: Tests for the `slarc pull` command through the CLI with a real archive on disk.
# ABOUTME: Slack, credentials, the clock and the downloader are fakes swapped in through bootstrap.
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest
from typer.testing import CliRunner

from slarc import bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.cli import app
from slarc.domain.envelope import decode_line
from slarc.domain.tracking import RunRecord, run_from_envelope
from slarc.ports import AuthFailed, BlobStore, CredentialReadError, Session
from slarc.service.downloads import QUEUED_STATUSES
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


class Fixture:
    def __init__(self, work: Path, home: Path) -> None:
        self.work = work
        self.home = home
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
        lines = (self.work / "runs.jsonl").read_bytes().splitlines()
        return [run_from_envelope(decode_line(line)) for line in lines]

    def archive_lines(self) -> int:
        files = [*self.work.glob("raw/**/*.jsonl"), *self.work.glob("directory/**/*.jsonl")]
        return sum(len(path.read_bytes().splitlines()) for path in files)


@pytest.fixture
def f(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fixture]:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    yield Fixture(work, tmp_path / "home")


def test_pull_creates_archive_after_confirmation(f: Fixture):
    result = f.pull(BASE, input="y\n")
    assert result.exit_code == 0, result.output
    assert f"{PROMPT} {f.work}? [Y/n]" in result.output
    assert (f.work / "slarc.toml").is_file()
    assert list((f.work / "raw" / TEAM / CHANNEL).glob("*.jsonl"))


def test_pull_declined_creates_nothing(f: Fixture):
    result = f.pull(BASE, input="n\n")
    assert result.exit_code == 1
    assert "No archive created." in result.output
    assert list(f.work.iterdir()) == []
    assert f.gateway.calls == []


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
    assert (f.work / "runs.jsonl").is_file()
    assert not (nested / "runs.jsonl").exists()


def test_pull_root_without_slarc_toml_creates_archive_after_confirmation(f: Fixture, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    result = f.pull("--root", str(other), BASE, input="y\n")
    assert result.exit_code == 0, result.output
    assert f"No archive at {other}. Create one? [Y/n]" in result.output
    assert (other / "slarc.toml").is_file()
    assert (other / "runs.jsonl").is_file()
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
    assert (other / "runs.jsonl").is_file()


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
    expected = f"Pulled 1 item: {f.archive_lines()} new lines, {pending} downloads pending."
    assert expected in result.stdout


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
