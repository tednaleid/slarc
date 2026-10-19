# ABOUTME: Auth use cases: list the Slack sessions found with who the user is in each workspace,
# ABOUTME: and check a pasted token and cookie with auth.test before storing them in the keychain.
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from slarc.ports import AuthFailed, NoSession, Session, SlackConnector, SlackGateway, SlarcError

PASTED_SOURCE = "keychain"


@dataclass(frozen=True)
class AuthStatus:
    """One session found and what auth.test said about it.

    error is set when Slack rejected the session. shadowed_by names the source of an earlier
    session that serves every workspace, which leaves this one unused.
    """

    source: str
    domain: str
    team_id: str
    team: str
    user: str
    user_id: str
    error: str | None = None
    shadowed_by: str | None = None


class SessionStore(Protocol):
    def store(self, session: Session) -> None: ...


def auth_status(connector: SlackConnector, workspace: str | None = None) -> list[AuthStatus]:
    sessions = connector.sessions()
    if workspace is not None:
        sessions = [s for s in sessions if s.matches(workspace)]
        if not sessions:
            raise NoSession(workspace)
    statuses = []
    serves_any: Session | None = None
    for session in sessions:
        if serves_any is not None:
            statuses.append(_unverified(session, shadowed_by=serves_any.source))
            continue
        try:
            info = connector.connect(session.team_id or session.domain).auth_test()
        except SlarcError as error:
            statuses.append(_unverified(session, error=str(error)))
        else:
            statuses.append(_verified(session, info))
        if session.serves_any_workspace:
            serves_any = session
    return statuses


def store_pasted(
    keyring_source: SessionStore,
    xoxc: str,
    xoxd: str,
    gateway_factory: Callable[[Session], SlackGateway],
) -> Session:
    xoxc, xoxd = xoxc.strip(), xoxd.strip()
    if not xoxc.startswith("xoxc-"):
        raise SlarcError("The token must start with xoxc-.")
    if not xoxd.startswith("xoxd-"):
        raise SlarcError("The d cookie must start with xoxd-.")
    pasted = Session("", "", "", PASTED_SOURCE, xoxc, xoxd)
    try:
        info = gateway_factory(pasted).auth_test()
    except AuthFailed:
        raise SlarcError(
            "Slack rejected the pasted token and cookie. "
            "Copy both again from a signed-in browser tab and rerun `slarc auth --paste`."
        ) from None
    session = Session(
        team_id=info["team_id"],
        domain=_domain(info),
        name=info.get("team", ""),
        source=PASTED_SOURCE,
        xoxc=xoxc,
        xoxd=xoxd,
    )
    try:
        keyring_source.store(session)
    except Exception as error:
        # Keychain backends raise their own error types, which the service layer cannot import.
        raise SlarcError(
            f"Could not store the token and cookie in the OS keychain ({type(error).__name__})."
        ) from None
    return session


def _domain(info: dict) -> str:
    host = urlsplit(info.get("url", "")).hostname or ""
    return host.split(".")[0]


def _unverified(
    session: Session, error: str | None = None, shadowed_by: str | None = None
) -> AuthStatus:
    return AuthStatus(
        source=session.source,
        domain=session.domain,
        team_id=session.team_id,
        team=session.name,
        user="",
        user_id="",
        error=error,
        shadowed_by=shadowed_by,
    )


def _verified(session: Session, info: dict) -> AuthStatus:
    return AuthStatus(
        source=session.source,
        domain=_domain(info) or session.domain,
        team_id=info.get("team_id", session.team_id),
        team=info.get("team", session.name),
        user=info.get("user", ""),
        user_id=info.get("user_id", ""),
    )
