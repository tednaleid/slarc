# ABOUTME: Ports between slarc's service layer and its adapters, and the value types they pass.
# ABOUTME: Also defines SlarcError and its subclasses, whose str() is the message shown to the user.
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from slarc.domain.envelope import Envelope, Kind
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem

PASTE_HINT = "Run `slarc auth --paste` to store a token and cookie instead."


class SlarcError(Exception):
    """An expected failure whose message is shown to the user without a stack trace."""


class NoArchive(SlarcError):
    def __init__(self) -> None:
        super().__init__("No slarc archive found here or in any parent directory.")


class NoSession(SlarcError):
    def __init__(self, workspace: str, message: str | None = None) -> None:
        self.workspace = workspace
        super().__init__(
            message
            or f"No Slack session found for workspace {workspace}. "
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


class ArchiveWriteFailed(SlarcError):
    def __init__(self, path: Path, reason: str | None) -> None:
        self.path = path
        super().__init__(f"Could not write to {path}: {reason}.")


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

    @property
    def serves_any_workspace(self) -> bool:
        return not self.domain and not self.team_id

    def matches(self, workspace: str) -> bool:
        return self.serves_any_workspace or workspace in (self.domain, self.team_id)


class CredentialSource(Protocol):
    def sessions(self) -> list[Session]: ...


class SlackGateway(Protocol):
    def auth_test(self) -> dict: ...
    def conversation_info(self, channel: str) -> dict: ...  # raises NotVisible
    def list_conversations(self) -> Iterator[dict]: ...
    def history(
        self, channel: str, oldest: str | None, latest: str | None
    ) -> Iterator[list[dict]]: ...  # pages, newest first
    def replies(self, channel: str, ts: str) -> list[dict]: ...  # raises NotVisible, ThreadNotFound
    def search(self, query: str) -> Iterator[dict]: ...  # matches across pages
    def users(self) -> Iterator[dict]: ...
    def usergroups(self) -> list[dict]: ...
    def emoji(self) -> dict[str, str]: ...
    def bot_info(self, bot_id: str) -> dict: ...
    def file_info(self, file_id: str) -> dict: ...  # the file object; raises FileDeleted
    @property
    def rate_limit_wait_s(self) -> float: ...  # seconds its calls waited for the limiter
    @property
    def api_calls(self) -> dict[str, int]: ...  # calls made per Web API method, pages included


class SlackConnector(Protocol):
    def sessions(self) -> list[Session]: ...
    def connect(self, workspace: str) -> SlackGateway: ...  # raises NoSession, AuthFailed


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


@dataclass(frozen=True)
class DownloadJob:
    team_id: str
    target_kind: Literal["file", "image"]
    target: str
    url: str
    max_bytes: int
    slack_auth: bool


@dataclass(frozen=True)
class DownloadResult:
    job: DownloadJob
    status: Literal["ok", "failed", "forbidden", "too_large", "deleted"]
    sha256: str | None
    size: int | None
    content_type: str | None
    error: str | None


class Downloader(Protocol):
    def fetch(self, jobs: list[DownloadJob], parallelism: int) -> Iterator[DownloadResult]: ...


class BlobStore(Protocol):
    def write(self, chunks: Iterable[bytes]) -> tuple[str, int]: ...
    def exists(self, sha256: str) -> bool: ...
    def read(self, sha256: str) -> bytes: ...
    def ids(self) -> Iterator[str]: ...
    def delete(self, sha256: str) -> None: ...  # a missing blob is not an error


class ExportWriter(Protocol):
    """Writes one file of an export, creating its directories and replacing an existing file."""

    def write(self, path: Path, data: bytes) -> None: ...


class ArchiveStore(Protocol):
    def append(self, env: Envelope) -> None: ...
    def flush(self) -> None: ...
    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]: ...
    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]: ...
    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]: ...
    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]: ...
    def removed_items(self) -> dict[tuple[str, str, str, str], TrackedItem]: ...
    def downloads(self) -> dict[tuple[str, str], DownloadState]: ...
    def append_run(self, run: RunRecord, slarc_version: str) -> None: ...
    def channels(self, team: str) -> list[str]: ...  # channels with stored message lines
    def message_lines(self, team: str, channel: str) -> Iterator[Envelope]: ...  # every version
    def drop_messages(self, team: str, channel: str, stamps: set[str]) -> int: ...
    def drop_directory_lines(self, team: str, kind: Kind, ids: set[str]) -> int: ...
    def drop_workspace(self, team: str) -> None: ...
    def blob_references(self) -> list[tuple[str, Kind, str, str]]: ...  # (team, kind, id, blob)


@dataclass(frozen=True)
class SearchHit:
    """A stored message matching a search; ts is the message's Slack ts, link its permalink."""

    author: str
    conversation: str
    ts: str
    snippet: str
    link: str


class QueryIndex(Protocol):
    def sql(self, query: str) -> tuple[list[str], list[tuple]]: ...
    def search(
        self, text: str, in_channel: str | None, from_user: str | None, limit: int
    ) -> list[SearchHit]: ...
    def conversation_counts(self) -> dict[tuple[str, str], int]: ...
    def messages(
        self, team: str, channel: str, since: str | None, until: str | None, include_deleted: bool
    ) -> list[dict]: ...
    def thread(
        self, team: str, channel: str, thread_ts: str, include_deleted: bool
    ) -> list[dict]: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Returns an aware UTC datetime."""
        ...


class Reporter(Protocol):
    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def progress(self, label: str, done: int, new: int, total: int | None) -> None: ...
    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None: ...
