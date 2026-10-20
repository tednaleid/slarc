# ABOUTME: Tests for the top-level slarc CLI behavior.
# ABOUTME: Runs the Typer app in-process with CliRunner.
from io import StringIO

from rich.console import Console
from typer.testing import CliRunner

from slarc import __version__
from slarc.cli import RichReporter, app

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
