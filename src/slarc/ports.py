# ABOUTME: Ports between slarc's service layer and its adapters, and the value types they pass.
# ABOUTME: Also defines SlarcError and its subclasses, whose str() is the message shown to the user.
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from slarc.domain.envelope import Envelope, Kind
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem

PASTE_HINT = "Run `slarc auth --paste` to store a token and cookie instead."


class SlarcError(Exception):
    """An expected failure whose message is shown to the user without a stack trace."""


class NoArchive(SlarcError):
    def __init__(self) -> None:
        super().__init__("No slarc archive found here or in any parent directory.")


class NoSession(SlarcError):
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        super().__init__(
            f"No Slack session found for workspace {workspace}. "
            "Sign into the Slack desktop app or run `slarc auth --paste`."
        )


AUTH_REFRESH_HINTS = {
    "desktop": "Sign into the Slack desktop app again.",
    "keychain": "Run `slarc auth --paste` to store a fresh token and cookie.",
    "env": "Set SLARC_XOXC and SLARC_XOXD to a fresh token and cookie.",
}


class AuthFailed(SlarcError):
    def __init__(self, source: str, team: str) -> None:
        self.source = source
        self.team = team
        hint = AUTH_REFRESH_HINTS.get(source, PASTE_HINT)
        super().__init__(f"Slack rejected the {source} credentials for workspace {team}. {hint}")


class NotVisible(SlarcError):
    def __init__(self, channel: str) -> None:
        self.channel = channel
        super().__init__(f"Conversation {channel} is not visible to this account.")


class ThreadNotFound(SlarcError):
    def __init__(self, channel: str, ts: str) -> None:
        self.channel = channel
        self.ts = ts
        super().__init__(f"Thread {ts} was not found in conversation {channel}.")


class FileDeleted(SlarcError):
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id
        super().__init__(f"File {file_id} has been deleted from Slack.")


class CredentialReadError(SlarcError):
    def __init__(self, source: str, step: str) -> None:
        self.source = source
        self.step = step
        super().__init__(f"Could not read {source} credentials: {step}. {PASTE_HINT}")


@dataclass(frozen=True)
class Session:
    team_id: str
    domain: str
    name: str
    source: str
    xoxc: str = field(repr=False)
    xoxd: str = field(repr=False)


class CredentialSource(Protocol):
    def sessions(self) -> list[Session]: ...


@dataclass(frozen=True)
class StoredMessage:
    """The latest stored line of a message; hash None means it was recorded as deleted."""

    hash: str | None
    thread_ts: str | None
    fetched_at: str


@dataclass(frozen=True)
class DirectoryState:
    hash: str
    blob: str | None


class BlobStore(Protocol):
    def write(self, chunks: Iterable[bytes]) -> tuple[str, int]: ...
    def exists(self, sha256: str) -> bool: ...
    def read(self, sha256: str) -> bytes: ...


class ArchiveStore(Protocol):
    def append(self, env: Envelope) -> None: ...
    def flush(self) -> None: ...
    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]: ...
    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]: ...
    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]: ...
    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]: ...
    def downloads(self) -> dict[tuple[str, str], DownloadState]: ...
    def append_run(self, run: RunRecord, slarc_version: str) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Returns an aware UTC datetime."""
        ...
