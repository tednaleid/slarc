# ABOUTME: Tests for the session-token Slack gateway and the connector that picks sessions.
# ABOUTME: A fake Web API client and a loopback server serve spike-shaped responses.
import json
import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import URLError

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import ConnectionErrorRetryHandler
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState

from slarc import __version__
from slarc.adapters.rate_limit import METHOD_TIERS
from slarc.adapters.retry_after import RetryAfterHandler
from slarc.adapters.slack_gateway import (
    RATE_LIMIT_RETRIES,
    READ_METHODS,
    SessionConnector,
    SessionTokenGateway,
    web_api,
)
from slarc.ports import (
    AuthFailed,
    FileDeleted,
    NoSession,
    NotVisible,
    Session,
    SlarcError,
    ThreadNotFound,
)
from tests.fakes import SPIKE_AUTH, FakeCredentialSource, FakeSlackGateway

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"
CHANNEL = "C0CA5396G03"
THREAD_TS = "1792410693.663869"

READ_ALLOWLIST = {
    "auth.test",
    "bots.info",
    "conversations.history",
    "conversations.info",
    "conversations.list",
    "conversations.replies",
    "emoji.list",
    "files.info",
    "rtm.connect",
    "search.messages",
    "usergroups.list",
    "users.info",
    "users.list",
}

DEFAULT_RESPONSES = {
    "auth.test": SPIKE_AUTH,
    "conversations.info": {"channel": {}},
    "conversations.list": {"channels": []},
    "conversations.history": {"messages": []},
    "conversations.replies": {"messages": []},
    "search.messages": {"messages": {"matches": [], "paging": {"page": 1, "pages": 1}}},
    "users.list": {"members": []},
    "users.info": {"user": {}},
    "rtm.connect": {"url": "wss://example.invalid/rtm"},
    "usergroups.list": {"usergroups": []},
    "emoji.list": {"emoji": {}},
    "bots.info": {"bot": {}},
    "files.info": {"file": {}},
}


def spike(name: str) -> dict:
    return json.loads((SPIKE / f"{name}.json").read_text())


class FakeWebApi:
    """Answers each method from a queue of responses, or with an error code, and records calls."""

    def __init__(
        self,
        responses: dict[str, list[dict]] | None = None,
        errors: dict[str, Exception | str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        error = self.errors.get(method)
        if isinstance(error, Exception):
            raise error
        if error is not None:
            raise SlackApiError(error, {"ok": False, "error": error})
        queue = self.responses.get(method)
        return queue.pop(0) if queue else {"ok": True, **DEFAULT_RESPONSES[method]}

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


class RecordingLimiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []

    def acquire(self, method: str) -> float:
        self.acquired.append(method)
        return 0.0


def make_session(domain: str = "naleidworkspace", team_id: str = "T0CB5LYPVFA") -> Session:
    return Session(team_id, domain, "naleid", "desktop", "xoxc-test-secret", "xoxd-test-secret")


def make_gateway(
    api: FakeWebApi, session: Session | None = None
) -> tuple[SessionTokenGateway, RecordingLimiter]:
    limiter = RecordingLimiter()
    gateway = SessionTokenGateway(session or make_session(), limiter, lambda _: api)
    return gateway, limiter


def page(response: dict, cursor: str) -> dict:
    return {**response, "response_metadata": {"next_cursor": cursor}}


def test_gateway_pages_history_through_limiter():
    messages = spike("conversations.history")["messages"]
    first = page({"ok": True, "messages": messages[:3], "has_more": True}, "cursor-2")
    second = page({"ok": True, "messages": messages[3:], "has_more": False}, "")
    api = FakeWebApi({"conversations.history": [first, second]})
    gateway, limiter = make_gateway(api)

    pages = list(gateway.history(CHANNEL, oldest="1792410000.000000", latest=None))

    assert pages == [messages[:3], messages[3:]]
    assert api.calls == [
        (
            "conversations.history",
            {"channel": CHANNEL, "limit": 999, "oldest": "1792410000.000000"},
        ),
        (
            "conversations.history",
            {"channel": CHANNEL, "limit": 999, "oldest": "1792410000.000000", "cursor": "cursor-2"},
        ),
    ]
    assert limiter.acquired == ["conversations.history", "conversations.history"]


def test_gateway_pages_replies_through_limiter():
    messages = spike("conversations.replies")["messages"]
    first = page({"ok": True, "messages": messages[:2], "has_more": True}, "cursor-2")
    second = {"ok": True, "messages": messages[2:], "has_more": False, "response_metadata": None}
    api = FakeWebApi({"conversations.replies": [first, second]})
    gateway, limiter = make_gateway(api)

    assert gateway.replies(CHANNEL, THREAD_TS) == messages
    assert [params for _, params in api.calls] == [
        {"channel": CHANNEL, "ts": THREAD_TS, "limit": 999},
        {"channel": CHANNEL, "ts": THREAD_TS, "limit": 999, "cursor": "cursor-2"},
    ]
    assert limiter.acquired == ["conversations.replies", "conversations.replies"]


def test_gateway_pages_search_through_limiter():
    response = spike("search.messages.0")
    matches = response["messages"]["matches"]
    first = {"ok": True, "messages": {"matches": matches[:2], "paging": {"page": 1, "pages": 2}}}
    second = {"ok": True, "messages": {"matches": matches[2:], "paging": {"page": 2, "pages": 2}}}
    api = FakeWebApi({"search.messages": [first, second]})
    gateway, limiter = make_gateway(api)

    assert list(gateway.search("in:#all-naleid")) == matches
    assert [params for _, params in api.calls] == [
        {"query": "in:#all-naleid", "count": 100, "sort": "timestamp", "page": 1},
        {"query": "in:#all-naleid", "count": 100, "sort": "timestamp", "page": 2},
    ]
    assert limiter.acquired == ["search.messages", "search.messages"]


class WaitingLimiter:
    def __init__(self, waits: list[float]) -> None:
        self.waits = waits

    def acquire(self, method: str) -> float:
        return self.waits.pop(0)


def test_gateway_totals_rate_limiter_waits():
    api = FakeWebApi()
    gateway = SessionTokenGateway(make_session(), WaitingLimiter([0.0, 1.5, 0.25]), lambda _: api)

    assert gateway.rate_limit_wait_s == 0.0
    gateway.auth_test()
    list(gateway.users())
    gateway.emoji()

    assert gateway.rate_limit_wait_s == 1.75


def test_gateway_counts_each_call_per_method():
    first = page({"ok": True, "messages": [], "has_more": True}, "cursor-2")
    api = FakeWebApi({"conversations.replies": [first]}, errors={"files.info": "file_deleted"})
    gateway, _ = make_gateway(api)

    assert gateway.api_calls == {}
    gateway.auth_test()
    gateway.auth_test()
    gateway.replies(CHANNEL, THREAD_TS)
    with pytest.raises(FileDeleted):
        gateway.file_info("F1")

    assert gateway.api_calls == {"auth.test": 1, "conversations.replies": 2, "files.info": 1}


def test_gateway_pages_directories_and_unwraps_objects():
    users = spike("users.list")["members"]
    api = FakeWebApi(
        {
            "users.list": [
                page({"ok": True, "members": users[:1]}, "cursor-2"),
                page({"ok": True, "members": users[1:]}, ""),
            ],
            "conversations.info": [spike("conversations.info")],
            "emoji.list": [spike("emoji.list")],
            "bots.info": [spike("bots.info.B0CA9EBAH1C")],
            "files.info": [spike("files.info.F0C9XQFTVRD")],
            "users.info": [{"ok": True, "user": users[1]}],
        }
    )
    gateway, _ = make_gateway(api)

    assert list(gateway.users()) == users
    assert [params for method, params in api.calls if method == "users.list"] == [
        {"limit": 1000},
        {"limit": 1000, "cursor": "cursor-2"},
    ]
    assert gateway.conversation_info(CHANNEL) == spike("conversations.info")["channel"]
    assert gateway.emoji() == spike("emoji.list")["emoji"]
    assert gateway.bot_info("B0CA9EBAH1C") == spike("bots.info.B0CA9EBAH1C")["bot"]
    assert gateway.file_info("F0C9XQFTVRD") == spike("files.info.F0C9XQFTVRD")["file"]
    assert gateway.user_info(users[1]["id"]) == users[1]
    assert ("users.info", {"user": users[1]["id"]}) in api.calls


ERROR_CASES = [
    ("conversations.history", "channel_not_found", lambda g: list(g.history(CHANNEL, None, None))),
    ("conversations.info", "channel_not_found", lambda g: g.conversation_info(CHANNEL)),
    ("conversations.replies", "not_in_channel", lambda g: g.replies(CHANNEL, THREAD_TS)),
    ("conversations.replies", "thread_not_found", lambda g: g.replies(CHANNEL, THREAD_TS)),
    ("files.info", "file_deleted", lambda g: g.file_info("F0C9XQFTVRD")),
    ("auth.test", "invalid_auth", lambda g: g.auth_test()),
    ("users.list", "not_authed", lambda g: list(g.users())),
    ("auth.test", "token_revoked", lambda g: g.auth_test()),
    ("auth.test", "token_expired", lambda g: g.auth_test()),
    ("auth.test", "account_inactive", lambda g: g.auth_test()),
]
EXPECTED = {
    "channel_not_found": NotVisible,
    "not_in_channel": NotVisible,
    "thread_not_found": ThreadNotFound,
    "file_deleted": FileDeleted,
    "invalid_auth": AuthFailed,
    "not_authed": AuthFailed,
    "token_revoked": AuthFailed,
    "token_expired": AuthFailed,
    "account_inactive": AuthFailed,
}


@pytest.mark.parametrize(("method", "code", "call"), ERROR_CASES)
def test_gateway_maps_errors(method, code, call):
    gateway, _ = make_gateway(FakeWebApi(errors={method: code}))
    with pytest.raises(EXPECTED[code]) as exc:
        call(gateway)
    assert "secret" not in str(exc.value)


def test_gateway_error_details():
    gateway, _ = make_gateway(
        FakeWebApi(
            errors={
                "conversations.replies": "thread_not_found",
                "files.info": "file_deleted",
                "auth.test": "invalid_auth",
            }
        )
    )
    with pytest.raises(ThreadNotFound) as thread:
        gateway.replies(CHANNEL, THREAD_TS)
    assert (thread.value.channel, thread.value.ts) == (CHANNEL, THREAD_TS)
    with pytest.raises(FileDeleted) as deleted:
        gateway.file_info("F0C9XQFTVRD")
    assert deleted.value.file_id == "F0C9XQFTVRD"
    with pytest.raises(AuthFailed) as auth:
        gateway.auth_test()
    assert (auth.value.source, auth.value.team) == ("desktop", "naleidworkspace")


def test_gateway_reports_other_failures_as_slarc_errors():
    api = FakeWebApi(errors={"usergroups.list": "missing_scope", "emoji.list": URLError("down")})
    gateway, _ = make_gateway(api)
    with pytest.raises(SlarcError, match="missing_scope"):
        gateway.usergroups()
    with pytest.raises(SlarcError, match="Could not reach Slack"):
        gateway.emoji()


def test_gateway_reports_channel_errors_without_a_channel_as_slarc_errors():
    gateway, _ = make_gateway(FakeWebApi(errors={"search.messages": "channel_not_found"}))
    with pytest.raises(SlarcError) as exc:
        list(gateway.search(f"in:<#{CHANNEL}>"))
    assert type(exc.value) is SlarcError
    assert str(exc.value) == "Slack returned channel_not_found for search.messages."


def test_gateway_never_calls_mutating_methods():
    api = FakeWebApi()
    gateway, limiter = make_gateway(api)
    gateway.auth_test()
    gateway.conversation_info(CHANNEL)
    list(gateway.list_conversations())
    list(gateway.history(CHANNEL, None, None))
    gateway.replies(CHANNEL, THREAD_TS)
    list(gateway.search("hello"))
    list(gateway.users())
    gateway.usergroups()
    gateway.emoji()
    gateway.bot_info("B0CA9EBAH1C")
    gateway.file_info("F0C9XQFTVRD")
    gateway.user_info("U0CA61XJF2R")
    assert gateway.rtm_url() == "wss://example.invalid/rtm"

    assert set(api.methods()) == READ_ALLOWLIST
    assert limiter.acquired == api.methods()
    assert READ_METHODS <= READ_ALLOWLIST
    assert set(METHOD_TIERS) <= READ_ALLOWLIST


def test_gateway_caches_auth_test_and_learns_team_from_it():
    env = Session("", "", "", "env", "xoxc-test-secret", "xoxd-test-secret")
    api = FakeWebApi(
        {"auth.test": [dict(SPIKE_AUTH)]}, errors={"conversations.history": "invalid_auth"}
    )
    gateway, _ = make_gateway(api, env)

    assert gateway.auth_test()["team_id"] == "T0CB5LYPVFA"
    assert gateway.auth_test()["user"] == "contact"
    with pytest.raises(AuthFailed) as exc:
        list(gateway.history(CHANNEL, None, None))
    assert (exc.value.source, exc.value.team) == ("env", "T0CB5LYPVFA")
    assert api.methods() == ["auth.test", "conversations.history"]


def test_web_api_sends_token_cookie_and_honest_user_agent():
    client = web_api(make_session()).client
    assert client.token == "xoxc-test-secret"
    assert client.headers["Cookie"] == "d=xoxd-test-secret"
    assert (
        client.headers["User-Agent"] == f"slarc/{__version__} (+https://github.com/tednaleid/slarc)"
    )
    handlers = {type(handler) for handler in client.retry_handlers}
    assert handlers == {RetryAfterHandler, ConnectionErrorRetryHandler}


RATE_LIMITED_REQUEST = HttpRequest(
    method="GET", url="https://slack.com/api/conversations.history", headers={}
)


@pytest.mark.parametrize(
    ("headers", "waits"),
    [({"Retry-After": "3"}, [3, 2, 1, 0]), ({}, [1, 0])],
)
def test_a_rate_limited_response_counts_down_the_wait_slack_asks_for(
    headers: dict[str, str | list[str]], waits: list[int]
):
    shown: list[int] = []
    slept: list[float] = []
    handler = RetryAfterHandler(shown.append, RATE_LIMIT_RETRIES, slept.append)
    state = RetryState()
    response = HttpResponse(status_code=429, headers=headers)

    assert handler.can_retry(state=state, request=RATE_LIMITED_REQUEST, response=response)
    handler.prepare_for_next_attempt(state=state, request=RATE_LIMITED_REQUEST, response=response)

    assert shown == waits
    assert slept == [1] * (len(waits) - 1)
    assert state.current_attempt == 1


def test_rate_limited_retries_stop_after_the_limit():
    handler = RetryAfterHandler(lambda _: None, RATE_LIMIT_RETRIES, lambda _: None)
    state = RetryState(current_attempt=RATE_LIMIT_RETRIES)
    response = HttpResponse(status_code=429, headers={"Retry-After": "1"})

    assert not handler.can_retry(state=state, request=RATE_LIMITED_REQUEST, response=response)


def test_web_api_counts_down_rate_limit_waits_with_the_callback_it_is_given():
    shown: list[int] = []
    [handler] = [
        h
        for h in web_api(make_session(), shown.append).client.retry_handlers
        if isinstance(h, RetryAfterHandler)
    ]
    handler.sleep = lambda _: None
    response = HttpResponse(status_code=429, headers={"Retry-After": "2"})

    handler.prepare_for_next_attempt(
        state=RetryState(), request=RATE_LIMITED_REQUEST, response=response
    )

    assert shown == [2, 1, 0]


def test_session_repr_has_no_secrets():
    session = make_session()
    gateway, _ = make_gateway(FakeWebApi(), session)
    connector = SessionConnector(FakeCredentialSource([session]))
    for text in (repr(session), repr(gateway), repr(connector), repr(web_api(session))):
        assert "xoxc-test-secret" not in text
        assert "xoxd-test-secret" not in text


def connector_for(
    sessions: list[Session],
) -> tuple[SessionConnector, dict[Session, FakeSlackGateway]]:
    gateways = {session: FakeSlackGateway() for session in sessions}
    return SessionConnector(FakeCredentialSource(sessions), gateways.__getitem__), gateways


def test_connector_picks_session_by_domain_or_team_id():
    one = make_session("one", "T1")
    two = make_session("two", "T2")
    connector, gateways = connector_for([one, two])
    assert connector.connect("two") is gateways[two]
    assert connector.connect("T1") is gateways[one]
    assert gateways[one].calls == [("auth.test",)]
    assert connector.sessions() == [one, two]


def test_connector_uses_domainless_env_session_for_any_workspace():
    env = Session("", "", "", "env", "xoxc-env", "xoxd-env")
    desktop = make_session("one", "T1")
    connector, gateways = connector_for([env, desktop])
    assert connector.connect("one") is gateways[env]
    assert connector.connect("anything") is gateways[env]


def test_connector_raises_no_session_for_unknown_workspace():
    connector, _ = connector_for([make_session("one", "T1")])
    with pytest.raises(NoSession) as exc:
        connector.connect("other")
    assert exc.value.workspace == "other"


def test_connector_raises_auth_failed_from_auth_test():
    session = make_session("one", "T1")
    gateway = FakeSlackGateway(auth_error=AuthFailed("desktop", "one"))
    connector = SessionConnector(FakeCredentialSource([session]), lambda _: gateway)
    with pytest.raises(AuthFailed):
        connector.connect("one")


def test_fake_gateway_pages_and_fails_like_gateway():
    history = spike("conversations.history")["messages"]
    replies = spike("conversations.replies")["messages"]
    gateway = FakeSlackGateway({CHANNEL: history + replies[1:]}, page_size=4)

    pages = list(gateway.history(CHANNEL, oldest=None, latest=None))
    assert [len(p) for p in pages] == [4, 2]
    assert [m["ts"] for m in pages[0] + pages[1]] == [m["ts"] for m in history]
    assert gateway.replies(CHANNEL, THREAD_TS) == replies
    with pytest.raises(NotVisible):
        gateway.replies("C0NOTVISIBLE", THREAD_TS)
    with pytest.raises(ThreadNotFound):
        gateway.replies(CHANNEL, "1700000000.000000")


def test_connector_records_each_authenticated_session_by_team_id():
    env = Session("", "", "", "env", "xoxc-env", "xoxd-env")
    connector, _ = connector_for([env])
    assert connector.authenticated == {}
    connector.connect("naleidworkspace")
    assert connector.authenticated == {"T0CB5LYPVFA": env}


def test_connector_does_not_record_a_session_that_fails_auth_test():
    session = make_session("one", "T1")
    gateway = FakeSlackGateway(auth_error=AuthFailed("desktop", "one"))
    connector = SessionConnector(FakeCredentialSource([session]), lambda _: gateway)
    with pytest.raises(AuthFailed):
        connector.connect("one")
    assert connector.authenticated == {}


@contextmanager
def local_slack_api(body: dict) -> Generator[str]:
    payload = json.dumps(body).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/"
    finally:
        server.shutdown()
        server.server_close()


def test_debug_logging_never_shows_the_session_cookie(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    with local_slack_api({"ok": True, "team_id": "T0CB5LYPVFA"}) as base_url:
        api = web_api(
            Session("T0CB5LYPVFA", "naleidworkspace", "naleid", "desktop", "xoxc-test", "xoxd-test")
        )
        api.client.base_url = base_url
        assert api.call("auth.test", {})["ok"] is True
    assert "xoxd-test" not in caplog.text
    assert "xoxc-test" not in caplog.text


def test_usergroups_asks_for_members():
    api = FakeWebApi()
    gateway, _ = make_gateway(api)
    gateway.usergroups()
    assert api.calls == [("usergroups.list", {"include_users": True})]
