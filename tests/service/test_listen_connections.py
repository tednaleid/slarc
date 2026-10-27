# ABOUTME: Tests for Connections, which keeps one RTM connection per workspace alive: pings,
# ABOUTME: a missing pong as a lost connection, handovers before closing, and reconnect backoff.
from itertools import pairwise

import pytest

from slarc.ports import AuthFailed, ConnectionLost
from slarc.service.listen_connections import Connections
from tests.fakes import FakeClock, FakeEventConnection, FakeEventConnector

ACME = "acme"
MESSAGE = {"type": "message", "channel": "C0CHANNEL1", "ts": "1.000100", "text": "hi"}


class LoggedConnection(FakeEventConnection):
    def __init__(self, log: list[str], name: str, events: list[dict] | None = None) -> None:
        super().__init__(events or [])
        self.log = log
        self.name = name

    def close(self) -> None:
        self.log.append(f"close {self.name}")
        super().close()


class LoggedConnector(FakeEventConnector):
    def __init__(self, log: list[str], queued: list[FakeEventConnection | Exception]) -> None:
        super().__init__({ACME: queued})
        self.log = log

    def open(self, workspace: str) -> FakeEventConnection:
        self.log.append(f"open {len(self.opened) + 1}")
        return super().open(workspace)


def run(connections: Connections, clock: FakeClock, seconds: int) -> list[tuple[str, dict]]:
    """Polls once a second for the given seconds, collecting the events."""
    events = []
    for _ in range(seconds):
        events += connections.poll(0.1)
        clock.advance(1)
    return events


def test_events_arrive_tagged_with_their_workspace_and_hello_is_hidden():
    connector = FakeEventConnector({ACME: [FakeEventConnection([{"type": "hello"}, MESSAGE])]})
    connections = Connections(connector, FakeClock(), [ACME])

    assert run(connections, FakeClock(), 2) == [(ACME, MESSAGE)]


def test_pings_every_five_seconds():
    clock = FakeClock()
    connection = FakeEventConnection()
    connections = Connections(FakeEventConnector({ACME: [connection]}), clock, [ACME])

    events = run(connections, clock, 21)

    assert connection.pings == 4
    assert events == []


def test_missing_pong_counts_as_lost():
    clock = FakeClock()
    silent = FakeEventConnection(answers_pings=False)
    connector = FakeEventConnector({ACME: [silent, FakeEventConnection()]})
    connections = Connections(connector, clock, [ACME])

    run(connections, clock, 45)

    assert silent.closed
    assert connector.opened == [ACME, ACME]
    assert connections.take_lost() == {ACME}
    assert connections.take_lost() == set()


def test_a_closed_connection_reconnects_and_counts_as_lost():
    clock = FakeClock()
    dropped = FakeEventConnection([ConnectionLost("closed")])
    connector = FakeEventConnector({ACME: [dropped, FakeEventConnection()]})
    connections = Connections(connector, clock, [ACME])

    run(connections, clock, 10)

    assert connector.opened == [ACME, ACME]
    assert connections.take_lost() == {ACME}


def test_goodbye_opens_the_new_connection_before_closing_the_old():
    clock = FakeClock()
    log: list[str] = []
    old = LoggedConnection(log, "1", events=[{"type": "goodbye"}])
    connector = LoggedConnector(log, [old, LoggedConnection(log, "2")])
    connections = Connections(connector, clock, [ACME])

    run(connections, clock, 3)

    assert log == ["open 1", "open 2", "close 1"]
    assert connections.take_lost() == set()


def test_handover_after_seven_and_a_half_hours():
    clock = FakeClock()
    log: list[str] = []
    connector = LoggedConnector(log, [LoggedConnection(log, "1"), LoggedConnection(log, "2")])
    connections = Connections(connector, clock, [ACME])
    connections.poll(0.1)

    clock.advance(7.5 * 3600 - 10)
    run(connections, clock, 5)
    assert log == ["open 1"]
    run(connections, clock, 10)

    assert log == ["open 1", "open 2", "close 1"]
    assert connections.take_lost() == set()


def test_reconnects_back_off():
    clock = FakeClock()
    start = clock.now()
    attempts: list[float] = []

    class Failing(FakeEventConnector):
        def open(self, workspace: str) -> FakeEventConnection:
            attempts.append((clock.now() - start).total_seconds())
            raise ConnectionLost("down")

    connections = Connections(Failing(), clock, [ACME])
    run(connections, clock, 200)

    gaps = [later - earlier for earlier, later in pairwise(attempts)]
    assert gaps == [5, 10, 20, 40, 60, 60]


def test_rejected_credentials_propagate():
    connector = FakeEventConnector({ACME: [AuthFailed("desktop", ACME)]})
    connections = Connections(connector, FakeClock(), [ACME])

    with pytest.raises(AuthFailed):
        connections.poll(0.1)


def test_close_closes_every_connection():
    connection = FakeEventConnection()
    connections = Connections(FakeEventConnector({ACME: [connection]}), FakeClock(), [ACME])
    connections.poll(0.1)

    connections.close()

    assert connection.closed


def test_a_pong_waiting_in_the_socket_after_a_long_block_keeps_the_connection():
    clock = FakeClock()
    connection = FakeEventConnection(answers_pings=False)
    connector = FakeEventConnector({ACME: [connection]})
    connections = Connections(connector, clock, [ACME])
    run(connections, clock, 6)
    assert connection.pings == 1
    connection.events.append({"type": "pong", "reply_to": 1})

    clock.advance(60)  # another workspace's rtm.connect waited out its limit
    connections.poll(0.1)

    assert not connection.closed
    assert connector.opened == [ACME]
