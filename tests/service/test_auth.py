# ABOUTME: Tests for the auth service: listing sessions with auth.test and storing pasted pairs.
# ABOUTME: Runs against the fake connector and gateway; no Slack or keychain access.
import pytest

from slarc.ports import AuthFailed, NoSession, Session, SlarcError
from slarc.service.auth import AuthStatus, auth_status, store_pasted
from tests.fakes import SPIKE_AUTH, FakeConnector, FakeSlackGateway

NALEID = Session("T0CB5LYPVFA", "naleidworkspace", "naleid", "desktop", "xoxc-1", "xoxd-1")
OTHER = Session("T2", "other", "Other", "keychain", "xoxc-2", "xoxd-2")
OTHER_AUTH = {
    "ok": True,
    "url": "https://other.slack.com/",
    "team": "Other",
    "user": "ted",
    "team_id": "T2",
    "user_id": "U2",
}


def test_auth_status_lists_each_session():
    connector = FakeConnector(
        {NALEID: FakeSlackGateway(), OTHER: FakeSlackGateway(auth=OTHER_AUTH)}
    )
    assert auth_status(connector) == [
        AuthStatus("desktop", "naleidworkspace", "T0CB5LYPVFA", "naleid", "contact", "U0CA538UEN7"),
        AuthStatus("keychain", "other", "T2", "Other", "ted", "U2"),
    ]


def test_auth_status_limits_to_one_workspace():
    other_gateway = FakeSlackGateway(auth=OTHER_AUTH)
    connector = FakeConnector({NALEID: FakeSlackGateway(), OTHER: other_gateway})
    statuses = auth_status(connector, "naleidworkspace")
    assert [s.team_id for s in statuses] == ["T0CB5LYPVFA"]
    assert other_gateway.calls == []


def test_auth_status_for_unknown_workspace_raises_no_session():
    connector = FakeConnector({NALEID: FakeSlackGateway()})
    with pytest.raises(NoSession):
        auth_status(connector, "elsewhere")


def test_auth_failure_message_names_source():
    failing = FakeSlackGateway(auth_error=AuthFailed("keychain", "other"))
    connector = FakeConnector({NALEID: FakeSlackGateway(), OTHER: failing})
    ok, failed = auth_status(connector)
    assert ok.error is None
    assert failed.source == "keychain"
    assert failed.domain == "other"
    assert failed.error is not None
    assert "keychain" in failed.error
    assert "slarc auth --paste" in failed.error


def test_domainless_env_session_shadows_later_sessions():
    env = Session("", "", "", "env", "xoxc-env", "xoxd-env")
    desktop_gateway = FakeSlackGateway(auth=OTHER_AUTH)
    connector = FakeConnector({env: FakeSlackGateway(), OTHER: desktop_gateway})
    env_status, other_status = auth_status(connector)
    assert (env_status.source, env_status.team_id, env_status.user) == (
        "env",
        "T0CB5LYPVFA",
        "contact",
    )
    assert other_status.shadowed_by == "env"
    assert other_status.error is None
    assert desktop_gateway.calls == []


class RecordingStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.stored: list[Session] = []
        self.error = error

    def store(self, session: Session) -> None:
        if self.error is not None:
            raise self.error
        self.stored.append(session)


class GatewayFactory:
    def __init__(self, gateway: FakeSlackGateway) -> None:
        self.gateway = gateway
        self.sessions: list[Session] = []

    def __call__(self, session: Session) -> FakeSlackGateway:
        self.sessions.append(session)
        return self.gateway


def test_paste_stores_after_successful_auth_test():
    store = RecordingStore()
    factory = GatewayFactory(FakeSlackGateway(auth=SPIKE_AUTH))

    session = store_pasted(store, " xoxc-pasted ", "xoxd-pasted\n", factory)

    assert session == Session(
        "T0CB5LYPVFA", "naleidworkspace", "naleid", "keychain", "xoxc-pasted", "xoxd-pasted"
    )
    assert store.stored == [session]
    assert [(s.xoxc, s.xoxd) for s in factory.sessions] == [("xoxc-pasted", "xoxd-pasted")]
    assert factory.gateway.calls == [("auth.test",)]


def test_paste_rejected_by_slack_stores_nothing():
    store = RecordingStore()
    factory = GatewayFactory(FakeSlackGateway(auth_error=AuthFailed("pasted", "unknown")))
    with pytest.raises(SlarcError) as exc:
        store_pasted(store, "xoxc-pasted", "xoxd-pasted", factory)
    assert "rejected" in str(exc.value)
    assert "xoxc-pasted" not in str(exc.value)
    assert store.stored == []


def test_paste_requires_token_and_cookie_prefixes():
    factory = GatewayFactory(FakeSlackGateway())
    with pytest.raises(SlarcError, match="xoxc-"):
        store_pasted(RecordingStore(), "xoxb-wrong", "xoxd-pasted", factory)
    with pytest.raises(SlarcError, match="xoxd-"):
        store_pasted(RecordingStore(), "xoxc-pasted", "wrong", factory)
    assert factory.sessions == []


def test_paste_keychain_failure_is_plain_error():
    store = RecordingStore(error=RuntimeError("backend said xoxc-pasted"))
    factory = GatewayFactory(FakeSlackGateway())
    with pytest.raises(SlarcError) as exc:
        store_pasted(store, "xoxc-pasted", "xoxd-pasted", factory)
    assert "OS keychain" in str(exc.value)
    assert "xoxc-pasted" not in str(exc.value)
