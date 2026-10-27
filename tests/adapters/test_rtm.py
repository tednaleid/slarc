# ABOUTME: Tests for RtmConnector over a fake websocket: the handshake cookie, waiting for hello,
# ABOUTME: timeouts, closed sockets, pings as the only frames sent, and errors that hide the URL.
import json
from collections.abc import Callable

import pytest
import websocket

from slarc.adapters.rtm import RtmConnector
from slarc.ports import ConnectionLost, Session
from tests.fakes import FakeCredentialSource, FakeSlackGateway

NALEID = Session("T0CB5LYPVFA", "naleidworkspace", "naleid", "desktop", "xoxc-1", "xoxd-1")
RTM_URL = "wss://wss-primary.slack.com/websocket/secret-ticket"
MESSAGE = {"type": "message", "channel": "C0CA5396G03", "ts": "1.000100", "text": "hi"}


class FakeSocket:
    """Serves frames in order; an exception in frames is raised from recv instead."""

    def __init__(self, frames: list[object]) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.closed = False

    def settimeout(self, seconds: float) -> None:
        self.timeouts.append(seconds)

    def recv(self) -> str:
        if not self.frames:
            raise websocket.WebSocketTimeoutException("timed out")
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return json.dumps(frame)

    def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def close(self) -> None:
        self.closed = True


class RtmGateway(FakeSlackGateway):
    def rtm_url(self) -> str:
        self.calls.append(("rtm.connect",))
        return RTM_URL


class FakeSessions:
    """A SessionConnector stand-in over one session and one gateway."""

    def __init__(self) -> None:
        self.gateway = RtmGateway()
        self.authenticated: dict[str, Session] = {}
        self.source = FakeCredentialSource([NALEID])

    def sessions(self) -> list[Session]:
        return [NALEID]

    def connect(self, workspace: str) -> RtmGateway:
        self.authenticated[self.gateway.auth_test()["team_id"]] = NALEID
        return self.gateway


def connector(socket: FakeSocket, opened: list[tuple[str, dict]]) -> RtmConnector:
    def create(url: str, **kwargs: object) -> FakeSocket:
        opened.append((url, kwargs))
        return socket

    return RtmConnector(FakeSessions(), create=create)


def test_open_sends_the_cookie_and_waits_for_hello() -> None:
    socket = FakeSocket([{"type": "hello"}, MESSAGE])
    opened: list[tuple[str, dict]] = []

    connection = connector(socket, opened).open("naleidworkspace")

    [(url, kwargs)] = opened
    assert url == RTM_URL
    assert "Cookie: d=xoxd-1" in kwargs["header"]
    assert connection.receive(1.0) == MESSAGE


def test_receive_timeout_returns_none() -> None:
    connection = connector(FakeSocket([{"type": "hello"}]), []).open("naleidworkspace")

    assert connection.receive(0.5) is None


def test_closed_socket_raises_connection_lost() -> None:
    closed = websocket.WebSocketConnectionClosedException("closed")
    connection = connector(FakeSocket([{"type": "hello"}, closed]), []).open("naleidworkspace")

    with pytest.raises(ConnectionLost):
        connection.receive(1.0)


def test_connection_sends_nothing_but_pings() -> None:
    socket = FakeSocket([{"type": "hello"}])
    connection = connector(socket, []).open("naleidworkspace")

    connection.ping()
    connection.ping()
    connection.close()

    assert socket.sent == [{"id": 1, "type": "ping"}, {"id": 2, "type": "ping"}]
    assert socket.closed


@pytest.mark.parametrize(
    "failure",
    [
        lambda: websocket.WebSocketBadStatusException(f"Handshake to {RTM_URL} failed", 403),
        lambda: OSError(f"cannot reach {RTM_URL}"),
    ],
)
def test_the_rtm_url_is_never_in_an_error(failure: Callable[[], Exception]) -> None:
    def create(url: str, **kwargs: object) -> FakeSocket:
        raise failure()

    with pytest.raises(ConnectionLost) as raised:
        RtmConnector(FakeSessions(), create=create).open("naleidworkspace")

    assert "secret-ticket" not in str(raised.value)
    assert "naleidworkspace" in str(raised.value)


def test_open_without_hello_raises_connection_lost() -> None:
    with pytest.raises(ConnectionLost):
        connector(FakeSocket([MESSAGE]), []).open("naleidworkspace")
