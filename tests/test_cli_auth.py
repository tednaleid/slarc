# ABOUTME: Tests for the `slarc auth` command through the CLI with fake services.
# ABOUTME: The bootstrap override swaps in fake credentials, keyring and gateways.
from typer.testing import CliRunner

from slarc import bootstrap
from slarc.adapters.credentials import CompositeSource, KeyringSource
from slarc.cli import app
from slarc.ports import AuthFailed, CredentialReadError, Session
from tests.fakes import FakeCredentialSource, FakeKeyring, FakeSlackGateway

runner = CliRunner()
NALEID = Session("T0CB5LYPVFA", "naleidworkspace", "naleid", "desktop", "xoxc-1", "xoxd-1")


def services(
    source: FakeCredentialSource, gateway: FakeSlackGateway, keyring: FakeKeyring | None = None
) -> bootstrap.Services:
    return bootstrap.Services(
        credentials=CompositeSource([source]),
        keychain=KeyringSource(keyring or FakeKeyring()),
        gateway_factory=lambda _, __: gateway,
    )


def test_cli_auth_prints_user_and_team():
    with bootstrap.override(services(FakeCredentialSource([NALEID]), FakeSlackGateway())):
        result = runner.invoke(app, ["auth", "naleidworkspace"])
    assert result.exit_code == 0, result.output
    assert "contact" in result.output
    assert "T0CB5LYPVFA" in result.output
    assert "desktop" in result.output


def test_cli_auth_failure_names_source_and_exits_1():
    gateway = FakeSlackGateway(auth_error=AuthFailed("desktop", "naleidworkspace"))
    with bootstrap.override(services(FakeCredentialSource([NALEID]), gateway)):
        result = runner.invoke(app, ["auth"])
    assert result.exit_code == 1
    assert "Slack rejected the desktop credentials" in result.output
    assert "Traceback" not in result.output


def test_cli_auth_without_sessions_shows_credential_warnings():
    error = CredentialReadError("desktop", "keychain read refused")
    with bootstrap.override(services(FakeCredentialSource(error=error), FakeSlackGateway())):
        result = runner.invoke(app, ["auth", "naleidworkspace"])
    assert result.exit_code == 1
    assert "keychain read refused" in result.output
    assert "No Slack session found for workspace naleidworkspace" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_auth_paste_stores_pair_without_echoing_it():
    keyring = FakeKeyring()
    fake = services(FakeCredentialSource(), FakeSlackGateway(), keyring)
    with bootstrap.override(fake):
        result = runner.invoke(app, ["auth", "--paste"], input="xoxc-pasted\nxoxd-pasted\n")
    assert result.exit_code == 0, result.output
    assert "naleidworkspace" in result.output
    assert "xoxc-pasted" not in result.output
    assert "xoxd-pasted" not in result.output
    assert [s.team_id for s in KeyringSource(keyring).sessions()] == ["T0CB5LYPVFA"]
