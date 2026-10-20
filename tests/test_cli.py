# ABOUTME: Tests for the top-level slarc CLI behavior.
# ABOUTME: Runs the Typer app in-process with CliRunner.
import signal
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from slarc import __version__, bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.cli import RichReporter, app
from tests.fakes import FakeKeyring
from tests.recorded_fixtures import recorded_lake

runner = CliRunner()


def test_version_prints_installed_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"slarc {__version__}"


def test_progress_bar_shows_messages_read_and_new():
    output = StringIO()
    with RichReporter(Console(file=output, width=100)) as reporter:
        reporter.progress("#general", 3, 0, 3)
    assert "#general" in output.getvalue()
    assert "3 read, 0 new" in output.getvalue()


def test_sigterm_interrupts_like_ctrl_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    handlers = {}
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.update({signum: handler})
    )
    runner.invoke(app, ["list", "--root", str(tmp_path)])

    with pytest.raises(KeyboardInterrupt):
        handlers[signal.SIGTERM](signal.SIGTERM, None)


def test_cli_take_ownership_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = recorded_lake(tmp_path / "archive")
    services = bootstrap.Services(
        credentials=CompositeSource([]),
        keychain=KeyringSource(FakeKeyring()),
        gateway_factory=lambda _: pytest.fail("this command never connects to Slack"),
        env={},
        home=tmp_path,
    )

    def on(host: str, *args: str):
        monkeypatch.setattr(bootstrap, "_this_host", lambda: (f"machine-{host}", host))
        with bootstrap.override(services):
            return runner.invoke(app, [*args, "sql", "SELECT 1", "--root", str(root)])

    assert on("alpha").exit_code == 0
    refused = on("beta")
    assert refused.exit_code == 1
    assert "This archive is owned by alpha." in refused.output
    assert "--take-ownership" in refused.output
    taken = on("beta", "--take-ownership")
    assert taken.exit_code == 0, taken.output
    assert on("beta").exit_code == 0
    assert "This archive is owned by beta." in on("alpha").output
    help_text = runner.invoke(app, ["--help"], env={"COLUMNS": "300"}).output
    assert "--take-ownership" in help_text
    assert (
        "Take this archive over from the machine that last used it, "
        "once your synced folder has finished syncing."
    ) in help_text
