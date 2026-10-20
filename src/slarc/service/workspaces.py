# ABOUTME: Opens one Slack connection per workspace in a pull run and records its auth entry.
# ABOUTME: Also serves the run's connections to the download drain as a SlackConnector.
from collections import Counter
from dataclasses import dataclass

from slarc.ports import NoSession, Session, SlackConnector, SlackGateway
from slarc.service.appends import fetched_now
from slarc.service.auth import domain_of
from slarc.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class Connection:
    team_id: str
    user_id: str
    domain: str
    gateway: SlackGateway


class Workspaces:
    """Connects once per workspace in a run, since each connection has its own rate limiter.

    Each connection is recorded in the run's teams and auth entries. As a SlackConnector it
    hands the run's connections to the download drain.
    """

    def __init__(self, connector: SlackConnector, uow: UnitOfWork) -> None:
        self._connector = connector
        self._uow = uow
        self._team_of: dict[str, str] = {}
        self._connections: dict[str, Connection] = {}

    def sessions(self) -> list[Session]:
        return self._connector.sessions()

    def connect(self, workspace: str) -> SlackGateway:
        return self.connection_to(workspace).gateway

    def rate_limit_wait_s(self) -> float:
        return sum(c.gateway.rate_limit_wait_s for c in self._connections.values())

    def api_calls(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        for connection in self._connections.values():
            total.update(connection.gateway.api_calls)
        return dict(sorted(total.items()))

    def connection_to(self, workspace: str) -> Connection:
        """Takes a domain or team id."""
        team_id = self._team_of.get(workspace, workspace)
        if team_id not in self._connections:
            connection = self._open(workspace)
            team_id = connection.team_id
            self._connections[team_id] = connection
        self._team_of[workspace] = team_id
        return self._connections[team_id]

    def _open(self, workspace: str) -> Connection:
        gateway = self._connector.connect(workspace)
        info = gateway.auth_test()
        session = next((s for s in self._connector.sessions() if s.matches(workspace)), None)
        if session is not None and session.serves_any_workspace:
            _require_workspace(workspace, session, info)
        domain = (session.domain if session is not None else "") or domain_of(info)
        run = self._uow.run
        run.teams.append(info["team_id"])
        run.auth.append(
            {
                "team_id": info["team_id"],
                "user_id": info["user_id"],
                "domain": domain,
                "name": info.get("team", ""),
                "credential_source": session.source if session is not None else "",
                "auth_test_at": fetched_now(self._uow),
            }
        )
        return Connection(info["team_id"], info["user_id"], domain, gateway)


def _require_workspace(workspace: str, session: Session, info: dict) -> None:
    """Refuses a session that serves any workspace when auth.test names a different one."""
    domain, team_id = domain_of(info), info["team_id"]
    if workspace not in (domain, team_id):
        actual = domain or team_id
        raise NoSession(
            workspace,
            f"The {session.source} credentials are for workspace {actual}, not {workspace}. "
            f"Unset them to use another session, or set SLARC_DOMAIN to {actual} so they "
            "apply only there.",
        )
