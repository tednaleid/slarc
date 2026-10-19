# ABOUTME: Tests for the top-level slarc CLI behavior.
# ABOUTME: Runs the Typer app in-process with CliRunner.
from typer.testing import CliRunner

from slarc import __version__
from slarc.cli import app

runner = CliRunner()


def test_version_prints_installed_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"slarc {__version__}"
