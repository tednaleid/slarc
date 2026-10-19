# ABOUTME: Credential sources for environment variables and the OS keychain, plus a composite.
# ABOUTME: The composite merges sessions in priority order and keeps errors of skipped sources.
import json
from collections.abc import Mapping
from typing import Protocol

from keyring.errors import KeyringError

from slarc.ports import CredentialReadError, CredentialSource, Session

ENV_SOURCE = "env"
ENV_XOXC = "SLARC_XOXC"
ENV_XOXD = "SLARC_XOXD"
ENV_DOMAIN = "SLARC_DOMAIN"

KEYCHAIN_SOURCE = "keychain"
KEYCHAIN_SERVICE = "slarc"
KEYCHAIN_INDEX = "teams"


class EnvSource:
    """A session from SLARC_XOXC and SLARC_XOXD, for SLARC_DOMAIN or any workspace if unset."""

    source = ENV_SOURCE

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def sessions(self) -> list[Session]:
        names = (ENV_XOXC, ENV_XOXD)
        missing = [name for name in names if not self._env.get(name)]
        if len(missing) == len(names):
            return []
        if missing:
            raise CredentialReadError(ENV_SOURCE, f"{missing[0]} not set")
        return [
            Session(
                team_id="",
                domain=self._env.get(ENV_DOMAIN, ""),
                name="",
                source=ENV_SOURCE,
                xoxc=self._env[ENV_XOXC],
                xoxd=self._env[ENV_XOXD],
            )
        ]


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


class KeyringSource:
    """Sessions stored in the OS keychain, one entry per team id plus an index of team ids."""

    source = KEYCHAIN_SOURCE

    def __init__(self, backend: KeyringBackend) -> None:
        self._backend = backend

    def sessions(self) -> list[Session]:
        try:
            sessions = []
            for team_id in self._team_ids():
                stored = self._backend.get_password(KEYCHAIN_SERVICE, team_id)
                if stored is None:
                    continue
                entry = json.loads(stored)
                sessions.append(
                    Session(
                        team_id=team_id,
                        domain=entry["domain"],
                        name=entry["name"],
                        source=KEYCHAIN_SOURCE,
                        xoxc=entry["xoxc"],
                        xoxd=entry["xoxd"],
                    )
                )
        except KeyringError:
            raise CredentialReadError(KEYCHAIN_SOURCE, "keychain read refused") from None
        except ValueError, KeyError, TypeError:
            raise CredentialReadError(KEYCHAIN_SOURCE, "stored entry is malformed") from None
        return sessions

    def store(self, session: Session) -> None:
        entry = {
            "domain": session.domain,
            "name": session.name,
            "xoxc": session.xoxc,
            "xoxd": session.xoxd,
        }
        self._backend.set_password(KEYCHAIN_SERVICE, session.team_id, json.dumps(entry))
        team_ids = self._team_ids()
        if session.team_id not in team_ids:
            team_ids.append(session.team_id)
            self._backend.set_password(KEYCHAIN_SERVICE, KEYCHAIN_INDEX, json.dumps(team_ids))

    def _team_ids(self) -> list[str]:
        stored = self._backend.get_password(KEYCHAIN_SERVICE, KEYCHAIN_INDEX)
        return json.loads(stored) if stored else []


class CompositeSource:
    """Sessions from several sources; the first source to supply a team id wins."""

    def __init__(self, sources: list[CredentialSource]) -> None:
        self._sources = sources
        self.errors: list[CredentialReadError] = []

    def sessions(self) -> list[Session]:
        self.errors = []
        by_team: dict[str, Session] = {}
        for source in self._sources:
            try:
                found = source.sessions()
            except CredentialReadError as error:
                self.errors.append(error)
                continue
            for session in found:
                by_team.setdefault(session.team_id, session)
        return list(by_team.values())
