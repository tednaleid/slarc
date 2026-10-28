# ABOUTME: Tests for the `slarc listen` command over a real archive on disk, with Slack, events
# ABOUTME: and downloads faked; a fake connection sends SIGINT once its events run out.
import dataclasses
import io
import re
import signal
from datetime import UTC
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from slarc import bootstrap, cli
from slarc.cli import app
from slarc.config import create_archive
from slarc.ports import ItemProgress
from tests.fakes import FakeClock, FakeEventConnection, FakeEventConnector
from tests.service.pull_harness import BASE, CHANNEL, TEAM, message
from tests.test_cli_pull import Fixture

runner = CliRunner()
NEW_TS = "1792500000.000100"


@pytest.fixture
def f(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ext_dir: Path) -> Fixture:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return Fixture(work, tmp_path / "home", ext_dir)


class InterruptWhenDone(FakeEventConnection):
    """Serves its events, then sends this process SIGINT once, as Ctrl-C would."""

    def __init__(self, events: list[dict]) -> None:
        super().__init__(events)
        self.interrupted = False

    def receive(self, timeout: float) -> dict | None:
        event = super().receive(timeout)
        if event is None and not self.interrupted:
            self.interrupted = True
            signal.raise_signal(signal.SIGINT)
        return event


def listen(f: Fixture, events: list[dict], *args: str):
    connector = FakeEventConnector({TEAM: [InterruptWhenDone(events)]})
    services = dataclasses.replace(f.services(), event_connector=lambda _: connector)
    with bootstrap.override(services):
        return runner.invoke(app, ["listen", *args])


def test_cli_listen_prints_one_line_per_event_and_stops_cleanly(f: Fixture):
    assert f.pull("--yes", BASE).exit_code == 0
    pushed = {"type": "message", "channel": CHANNEL, **message(NEW_TS, text="hello")}

    result = listen(f, [pushed])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert [re.sub(r"^\d\d:\d\d ", "", line) for line in lines] == [
        "Pulling 1 tracked item before listening.",
        "Pulled 1 tracked item: 0 new messages.",
        "Listening to naleidworkspace (1 tracked item).",
        "#all-naleid  new message from Ted Naleid",
        "Stopped.",
    ]
    assert all(re.match(r"^\d\d:\d\d ", line) for line in lines)
    assert f.messages(CHANNEL) > 0


def test_cli_listen_with_nothing_tracked_exits_2(f: Fixture):
    create_archive(f.work)

    result = listen(f, [])

    assert result.exit_code == 2
    assert "No tracked items yet" in result.output


class InterruptTwice(FakeEventConnection):
    """Sends this process SIGINT twice in a row, as pressing Ctrl-C twice would."""

    def __init__(self) -> None:
        super().__init__()
        self.interrupted = False

    def receive(self, timeout: float) -> dict | None:
        if not self.interrupted:
            self.interrupted = True
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
        return super().receive(timeout)


def test_cli_listen_stops_at_once_on_a_second_interrupt(f: Fixture):
    assert f.pull("--yes", BASE).exit_code == 0
    connector = FakeEventConnector({TEAM: [InterruptTwice()]})
    services = dataclasses.replace(f.services(), event_connector=lambda _: connector)

    with bootstrap.override(services):
        result = runner.invoke(app, ["listen"])

    assert result.exit_code == 130
    assert "Interrupted" in result.stderr


def test_line_reporter_shows_progress_rows_only_during_a_pull(monkeypatch: pytest.MonkeyPatch):
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=stderr, force_terminal=True, width=120))
    reporter = cli.LineReporter(FakeClock(), UTC)

    reporter.progress("#before", ItemProgress(read=1, new=1))
    reporter.pull_started()
    reporter.progress("#general", ItemProgress(read=5, new=2))
    reporter.downloads(1, 0, 0, 2)
    reporter.waiting(30)
    reporter.pull_ended()
    reporter.downloads(3, 0, 0, 4)
    reporter.waiting(20)

    shown = stderr.getvalue()
    assert "#general" in shown and "5 read, 2 new" in shown
    assert "1/2" in shown and "rate limited, waiting 30s" in shown
    assert "#before" not in shown and "3/4" not in shown and "waiting 20s" not in shown


def test_line_reporter_shows_no_rows_when_stderr_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
):
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "err", Console(file=stderr, force_terminal=False))
    reporter = cli.LineReporter(FakeClock(), UTC)

    reporter.pull_started()
    reporter.progress("#general", ItemProgress(read=5, new=2))
    reporter.pull_ended()

    assert stderr.getvalue() == ""
