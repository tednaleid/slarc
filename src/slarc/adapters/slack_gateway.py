# ABOUTME: Read-only Slack Web API gateway over slack_sdk using a session token and d cookie.
# ABOUTME: Also the connector that picks a session for a workspace and checks it with auth.test.
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)

from slarc import __version__
from slarc.adapters.rate_limit import METHOD_TIERS, TieredLimiter
from slarc.ports import (
    AuthFailed,
    CredentialSource,
    FileDeleted,
    NoSession,
    NotVisible,
    Session,
    SlackGateway,
    SlarcError,
    ThreadNotFound,
)

READ_METHODS = frozenset(METHOD_TIERS)
USER_AGENT = f"slarc/{__version__}"
PAGE_LIMIT = 200
# Slack's largest page for history and replies; users.list accepted 1000 in a live check.
MESSAGE_PAGE_LIMIT = 999
USER_PAGE_LIMIT = 1000
SEARCH_COUNT = 100
CONVERSATION_TYPES = "public_channel,private_channel,mpim,im"
RATE_LIMIT_RETRIES = 3

NOT_VISIBLE_ERRORS = ("channel_not_found", "not_in_channel")
AUTH_ERRORS = ("invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive")


class WebApi(Protocol):
    def call(self, method: str, params: dict[str, Any]) -> dict: ...


class Limiter(Protocol):
    def acquire(self, method: str) -> float: ...


class SlackWebApi:
    """Calls Web API methods through a slack_sdk WebClient and returns the response body."""

    def __init__(self, client: WebClient) -> None:
        self.client = client

    def call(self, method: str, params: dict[str, Any]) -> dict:
        data = self.client.api_call(method, http_verb="GET", params=params).data
        if not isinstance(data, dict):
            raise SlarcError(f"Slack returned a response that is not JSON for {method}.")
        return data


def web_api(session: Session) -> SlackWebApi:
    client = WebClient(
        token=session.xoxc,
        headers={"Cookie": f"d={session.xoxd}"},
        retry_handlers=[
            RateLimitErrorRetryHandler(max_retry_count=RATE_LIMIT_RETRIES),
            ConnectionErrorRetryHandler(),
        ],
    )
    # WebClient replaces any User-Agent passed in headers with its own.
    client.headers["User-Agent"] = USER_AGENT
    return SlackWebApi(client)


class SessionTokenGateway:
    """SlackGateway over the Web API; only methods in READ_METHODS can be called."""

    def __init__(
        self,
        session: Session,
        limiter: Limiter,
        client_factory: Callable[[Session], WebApi] = web_api,
    ) -> None:
        self._session = session
        self._limiter = limiter
        self._api = client_factory(session)
        self._workspace = session.domain or session.team_id or "unknown"
        self._auth: dict | None = None
        self.rate_limit_wait_s = 0.0

    def auth_test(self) -> dict:
        if self._auth is None:
            self._auth = self._call("auth.test")
            self._workspace = self._session.domain or self._auth.get("team_id") or self._workspace
        return dict(self._auth)

    def conversation_info(self, channel: str) -> dict:
        return self._call("conversations.info", channel=channel)["channel"]

    def list_conversations(self) -> Iterator[dict]:
        pages = self._pages("conversations.list", types=CONVERSATION_TYPES, limit=PAGE_LIMIT)
        for response in pages:
            yield from response["channels"]

    def history(self, channel: str, oldest: str | None, latest: str | None) -> Iterator[list[dict]]:
        params: dict[str, Any] = {"channel": channel, "limit": MESSAGE_PAGE_LIMIT}
        if oldest is not None:
            params["oldest"] = oldest
        if latest is not None:
            params["latest"] = latest
        for response in self._pages("conversations.history", **params):
            yield response["messages"]

    def replies(self, channel: str, ts: str) -> list[dict]:
        pages = self._pages(
            "conversations.replies", channel=channel, ts=ts, limit=MESSAGE_PAGE_LIMIT
        )
        return [message for response in pages for message in response["messages"]]

    def search(self, query: str) -> Iterator[dict]:
        page = 1
        while True:
            response = self._call(
                "search.messages", query=query, count=SEARCH_COUNT, sort="timestamp", page=page
            )
            messages = response["messages"]
            yield from messages["matches"]
            if page >= messages["paging"]["pages"]:
                return
            page += 1

    def users(self) -> Iterator[dict]:
        for response in self._pages("users.list", limit=USER_PAGE_LIMIT):
            yield from response["members"]

    def usergroups(self) -> list[dict]:
        return self._call("usergroups.list")["usergroups"]

    def emoji(self) -> dict[str, str]:
        return self._call("emoji.list")["emoji"]

    def bot_info(self, bot_id: str) -> dict:
        return self._call("bots.info", bot=bot_id)["bot"]

    def file_info(self, file_id: str) -> dict:
        return self._call("files.info", file=file_id)["file"]

    def _pages(self, method: str, **params: Any) -> Iterator[dict]:
        cursor = ""
        while True:
            response = self._call(method, **params, **({"cursor": cursor} if cursor else {}))
            yield response
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return

    def _call(self, method: str, **params: Any) -> dict:
        if method not in READ_METHODS:
            raise ValueError(f"{method} is not a read method slarc may call")
        self.rate_limit_wait_s += self._limiter.acquire(method)
        try:
            return self._api.call(method, params)
        except SlackApiError as error:
            raise self._api_error(method, params, error.response.get("error")) from None
        except OSError:
            raise SlarcError(f"Could not reach Slack to call {method}.") from None

    def _api_error(self, method: str, params: dict[str, Any], code: str | None) -> SlarcError:
        if code in NOT_VISIBLE_ERRORS and "channel" in params:
            return NotVisible(params["channel"])
        if code == "thread_not_found":
            return ThreadNotFound(params["channel"], params["ts"])
        if code == "file_deleted":
            return FileDeleted(params["file"])
        if code in AUTH_ERRORS:
            return AuthFailed(self._session.source, self._workspace)
        return SlarcError(f"Slack returned {code} for {method}.")


def session_gateway(session: Session) -> SlackGateway:
    return SessionTokenGateway(session, TieredLimiter())


class SessionConnector:
    """Picks the first session, in source priority order, that serves a workspace.

    authenticated maps the team id auth.test returned to each session that passed it.
    """

    def __init__(
        self,
        source: CredentialSource,
        gateway_factory: Callable[[Session], SlackGateway] = session_gateway,
    ) -> None:
        self._source = source
        self._gateway_factory = gateway_factory
        self._sessions: list[Session] | None = None
        self.authenticated: dict[str, Session] = {}

    def sessions(self) -> list[Session]:
        if self._sessions is None:
            self._sessions = self._source.sessions()
        return self._sessions

    def connect(self, workspace: str) -> SlackGateway:
        session = next((s for s in self.sessions() if s.matches(workspace)), None)
        if session is None:
            raise NoSession(workspace)
        gateway = self._gateway_factory(session)
        self.authenticated[gateway.auth_test()["team_id"]] = session
        return gateway
