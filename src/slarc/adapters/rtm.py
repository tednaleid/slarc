# ABOUTME: Opens Slack's real-time event websocket through the documented rtm.connect, sending the
# ABOUTME: session's d cookie on the handshake; the connection sends nothing but pings.
import contextlib
import json
from collections.abc import Callable
from typing import Any, Protocol

import websocket

from slarc.adapters.slack_gateway import USER_AGENT
from slarc.ports import AuthFailed, ConnectionLost, NoSession, Session, SlackGateway, SlarcError

OPEN_TIMEOUT_S = 30.0


class WebSocket(Protocol):
    def settimeout(self, seconds: float) -> None: ...
    def recv(self) -> str | bytes: ...
    def send(self, text: str) -> Any: ...
    def close(self) -> None: ...


class Sessions(Protocol):
    authenticated: dict[str, Session]

    def connect(self, workspace: str) -> SlackGateway: ...


class RtmConnection:
    def __init__(self, socket: WebSocket, workspace: str) -> None:
        self._socket = socket
        self._workspace = workspace
        self._pings = 0

    def receive(self, timeout: float) -> dict | None:
        self._socket.settimeout(timeout)
        try:
            frame = self._socket.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except websocket.WebSocketException, OSError:
            raise ConnectionLost(f"The event connection to {self._workspace} closed.") from None
        # websocket-client answers a close frame and returns an empty frame.
        try:
            event = json.loads(frame) if frame else None
        except ValueError:
            event = None
        if not isinstance(event, dict):
            raise ConnectionLost(f"The event connection to {self._workspace} closed.")
        return event

    def ping(self) -> None:
        self._pings += 1
        try:
            self._socket.send(json.dumps({"id": self._pings, "type": "ping"}))
        except websocket.WebSocketException, OSError:
            raise ConnectionLost(f"The event connection to {self._workspace} closed.") from None

    def close(self) -> None:
        with contextlib.suppress(websocket.WebSocketException, OSError):
            self._socket.close()


class RtmConnector:
    """Opens one connection per call. Gateways are kept per workspace, so rtm.connect's limit of
    one call a minute holds across reconnects."""

    def __init__(
        self,
        sessions: Sessions,
        create: Callable[..., Any] = websocket.create_connection,
    ) -> None:
        self._sessions = sessions
        self._create = create
        self._gateways: dict[str, SlackGateway] = {}

    def open(self, workspace: str) -> RtmConnection:
        failed = ConnectionLost(f"Could not open the event connection to {workspace}.")
        try:
            gateway = self._gateways.get(workspace)
            if gateway is None:
                gateway = self._gateways[workspace] = self._sessions.connect(workspace)
            session = self._sessions.authenticated[gateway.auth_test()["team_id"]]
            url = gateway.rtm_url()
        except AuthFailed, NoSession:
            raise
        except SlarcError:
            # Slack unreachable or failing for now: the caller backs off and tries again.
            raise failed from None
        try:
            socket = self._create(
                url,
                header=[f"Cookie: d={session.xoxd}", f"User-Agent: {USER_AGENT}"],
                timeout=OPEN_TIMEOUT_S,
            )
        except websocket.WebSocketException, OSError:
            raise failed from None
        connection = RtmConnection(socket, workspace)
        hello = connection.receive(OPEN_TIMEOUT_S)
        if hello is None or hello.get("type") != "hello":
            connection.close()
            raise failed
        return connection
