# ABOUTME: Keeps one RTM connection per workspace alive for slarc listen: pings, treats a missing
# ABOUTME: pong as lost, hands over before Slack closes a connection, and reconnects with backoff.
from dataclasses import dataclass
from datetime import datetime, timedelta

from slarc.ports import Clock, ConnectionLost, EventConnection, EventConnector

PING_S = 5.0
PONG_DEADLINE_S = 30.0
HANDOVER_AFTER = timedelta(hours=7, minutes=30)
BACKOFF_S = (5, 10, 20, 40, 60)
READ_AHEAD = 100  # events read from one connection in one poll, after the first
QUICK_S = 0.01
HIDDEN = ("hello", "pong", "goodbye")


@dataclass
class _Link:
    connection: EventConnection | None = None
    opened_at: datetime | None = None
    last_ping: datetime | None = None
    awaiting_since: datetime | None = None  # the oldest ping with no pong yet
    retry_at: datetime | None = None
    failures: int = 0
    lost: bool = False  # dropped, or failed to open, since the last good open


class Connections:
    """take_lost names each workspace whose connection opened again after a drop or a failed
    open; events in between were never seen, so it needs a catch-up pull."""

    def __init__(self, connector: EventConnector, clock: Clock, workspaces: list[str]) -> None:
        self._connector = connector
        self._clock = clock
        self._links = {workspace: _Link() for workspace in workspaces}
        self._lost: set[str] = set()

    def poll(self, timeout: float) -> list[tuple[str, dict]]:
        """Opens connections that are due, keeps them alive and returns their events, waiting
        up to timeout for each connection's first. AuthFailed from an open propagates."""
        events: list[tuple[str, dict]] = []
        wait = timeout / max(1, len(self._links))
        for workspace, link in self._links.items():
            now = self._clock.now()
            if link.connection is None:
                if link.retry_at is not None and now < link.retry_at:
                    continue
                self._open(workspace, link, now)
                if link.connection is None:
                    continue
            try:
                # Reading first takes a pong that arrived while another open blocked the loop.
                events += [(workspace, event) for event in self._receive(workspace, link, wait)]
                self._keep_alive(workspace, link, self._clock.now())
            except ConnectionLost:
                self._drop(link, now)
        return events

    def take_lost(self) -> set[str]:
        lost, self._lost = self._lost, set()
        return lost

    def close(self) -> None:
        for link in self._links.values():
            if link.connection is not None:
                link.connection.close()
                link.connection = None

    def _open(self, workspace: str, link: _Link, now: datetime) -> None:
        try:
            connection = self._connector.open(workspace)
        except ConnectionLost:
            link.lost = True
            self._retry_later(link, now)
            return
        if link.lost:
            self._lost.add(workspace)
        self._attach(link, connection, now)

    def _attach(self, link: _Link, connection: EventConnection, now: datetime) -> None:
        link.connection = connection
        link.opened_at = link.last_ping = now
        link.awaiting_since = link.retry_at = None
        link.failures = 0
        link.lost = False

    def _keep_alive(self, workspace: str, link: _Link, now: datetime) -> None:
        assert link.connection is not None and link.last_ping is not None
        waited = link.awaiting_since
        if waited is not None and (now - waited).total_seconds() > PONG_DEADLINE_S:
            raise ConnectionLost(f"The event connection to {workspace} stopped answering.")
        if link.opened_at is not None and now - link.opened_at >= HANDOVER_AFTER:
            self._handover(workspace, link, now)
        if (now - link.last_ping).total_seconds() >= PING_S:
            link.connection.ping()
            link.last_ping = now
            if link.awaiting_since is None:
                link.awaiting_since = now

    def _receive(self, workspace: str, link: _Link, wait: float) -> list[dict]:
        events = []
        for _ in range(READ_AHEAD + 1):
            assert link.connection is not None
            event = link.connection.receive(wait if not events else QUICK_S)
            if event is None:
                break
            match event.get("type"):
                case "pong":
                    link.awaiting_since = None
                case "goodbye":
                    self._handover(workspace, link, self._clock.now())
                    break
                case kind if kind not in HIDDEN:
                    events.append(event)
        return events

    def _handover(self, workspace: str, link: _Link, now: datetime) -> None:
        """Opens the workspace's next connection, then closes the old one; a failed open counts
        as losing the old one."""
        old = link.connection
        assert old is not None
        new = self._connector.open(workspace)
        old.close()
        self._attach(link, new, now)

    def _drop(self, link: _Link, now: datetime) -> None:
        if link.connection is not None:
            link.connection.close()
            link.connection = None
        link.lost = True
        self._retry_later(link, now)

    def _retry_later(self, link: _Link, now: datetime) -> None:
        delay = BACKOFF_S[min(link.failures, len(BACKOFF_S) - 1)]
        link.retry_at = now + timedelta(seconds=delay)
        link.failures += 1
