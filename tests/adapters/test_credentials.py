# ABOUTME: Tests for the environment, keychain and composite credential sources.
# ABOUTME: The keychain source runs against an in-memory backend, never the real keychain.
import pytest
from keyring.errors import KeyringError

from slarc.adapters.credentials import CompositeSource, EnvSource, KeyringSource
from slarc.ports import CredentialReadError, Session


class FakeKeyring:
    def __init__(self):
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.entries.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.entries[(service_name, username)] = password


class RefusingKeyring(FakeKeyring):
    def get_password(self, service_name: str, username: str) -> str | None:
        raise KeyringError("locked")


class StaticSource:
    def __init__(self, sessions: list[Session]):
        self._sessions = sessions

    def sessions(self) -> list[Session]:
        return self._sessions


class FailingSource:
    def __init__(self, error: CredentialReadError):
        self.error = error

    def sessions(self) -> list[Session]:
        raise self.error


def session(team_id: str, source: str, xoxc: str = "xoxc-1") -> Session:
    return Session(team_id, f"{team_id.lower()}-domain", f"{team_id} name", source, xoxc, "xoxd-1")


def test_env_source_requires_both_values():
    with pytest.raises(CredentialReadError) as exc:
        EnvSource({"SLARC_XOXC": "xoxc-secret"}).sessions()
    assert exc.value.source == "env"
    assert "SLARC_XOXD" in str(exc.value)
    assert "xoxc-secret" not in str(exc.value)
    with pytest.raises(CredentialReadError) as exc:
        EnvSource({"SLARC_XOXD": "xoxd-secret", "SLARC_DOMAIN": "acme"}).sessions()
    assert "SLARC_XOXC" in str(exc.value)
    assert "xoxd-secret" not in str(exc.value)


def test_env_source_without_variables_has_no_sessions():
    assert EnvSource({}).sessions() == []


def test_env_source_names_workspace_by_domain():
    env = {"SLARC_XOXC": "xoxc-1", "SLARC_XOXD": "xoxd-1", "SLARC_DOMAIN": "acme"}
    assert EnvSource(env).sessions() == [Session("", "acme", "", "env", "xoxc-1", "xoxd-1")]


def test_env_source_without_domain_applies_to_any_workspace():
    env = {"SLARC_XOXC": "xoxc-1", "SLARC_XOXD": "xoxd-1"}
    assert EnvSource(env).sessions() == [Session("", "", "", "env", "xoxc-1", "xoxd-1")]


def test_keyring_store_and_read_round_trip():
    backend = FakeKeyring()
    source = KeyringSource(backend)
    first = Session("T1", "one", "One", "keychain", "xoxc-1", "xoxd-1")
    second = Session("T2", "two", "Two", "keychain", "xoxc-2", "xoxd-2")
    source.store(first)
    source.store(second)
    source.store(first)
    assert {username for service, username in backend.entries if service == "slarc"} >= {
        "T1",
        "T2",
    }
    assert KeyringSource(backend).sessions() == [first, second]


def test_keyring_source_stores_pasted_session_as_keychain_source():
    backend = FakeKeyring()
    KeyringSource(backend).store(Session("T1", "one", "One", "paste", "xoxc-1", "xoxd-1"))
    assert [s.source for s in KeyringSource(backend).sessions()] == ["keychain"]


def test_keyring_source_with_nothing_stored_has_no_sessions():
    assert KeyringSource(FakeKeyring()).sessions() == []


def test_keyring_refusal_raises_credential_read_error():
    with pytest.raises(CredentialReadError) as exc:
        KeyringSource(RefusingKeyring()).sessions()
    assert exc.value.source == "keychain"


def test_composite_first_source_wins():
    env = StaticSource([session("T1", "env", "xoxc-env")])
    desktop = StaticSource([session("T1", "desktop"), session("T2", "desktop")])
    sessions = CompositeSource([env, desktop]).sessions()
    assert [(s.team_id, s.source) for s in sessions] == [("T1", "env"), ("T2", "desktop")]


def test_composite_keeps_errors_and_continues():
    error = CredentialReadError("desktop", "keychain read refused")
    composite = CompositeSource([FailingSource(error), StaticSource([session("T1", "keychain")])])
    assert [s.team_id for s in composite.sessions()] == ["T1"]
    assert composite.errors == [error]


def test_session_repr_hides_secrets():
    text = repr(Session("T1", "one", "One", "env", "xoxc-secret", "xoxd-secret"))
    assert "xoxc-secret" not in text
    assert "xoxd-secret" not in text
