# ABOUTME: Ports between slarc's service layer and its adapters, and the value types they pass.
# ABOUTME: Also defines SlarcError and its subclasses, whose str() is the message shown to the user.
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from slarc.domain.envelope import Envelope, Kind
from slarc.domain.tracking import DownloadState, ItemKey, RunRecord, TrackedItem
from slarc.domain.window import Window

PASTE_HINT = "Run `slarc auth --paste` to store a token and cookie instead."

type DownloadKey = tuple[str, str]  # (team, target)
type DirectoryRef = tuple[str, Kind, str]  # (team, kind, id)


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


class ArchiveFailed(SlarcError):
    """The archive could not be read or written; a pull stops rather than skip items."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(message)


class ArchiveWriteFailed(ArchiveFailed):
    def __init__(self, path: Path, reason: str | None) -> None:
        super().__init__(path, f"Could not write to {path}: {reason}.")


class ArchiveReadFailed(ArchiveFailed):
    def __init__(self, path: Path, reason: str | None) -> None:
        super().__init__(path, f"Could not read {path}: {reason}.")


class FileDeleted(SlarcError):
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id
        super().__init__(f"File {file_id} has been deleted from Slack.")


class CommitConflict(SlarcError):
    def __init__(self) -> None:
        super().__init__("Another slarc command changed the archive at the same time.")


class MaintenanceFailed(SlarcError):
    def __init__(self, reason: str) -> None:
        super().__init__(
            f"The run is saved, but maintaining the archive failed ({reason}); "
            "the next pull or rm tries again."
        )


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
    status: Literal["ok", "failed", "forbidden", "not_found", "too_large", "deleted", "not_tried"]
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
    def delete_partial(self) -> None: ...  # what killed writers left; none may be running


class ExportWriter(Protocol):
    """Writes one file of an export, creating its directories and replacing an existing file."""

    def write(self, path: Path, data: bytes) -> None: ...


@dataclass(frozen=True)
class Commit:
    """What a commit writes along with the open batch, and the archive state it assumed.

    An item with revision 0 starts tracking; any other item, and each unchanged item, must
    still have the revision it was read with. In each complete team, the tracked items that
    are not removed must be exactly the items and unchanged items in that team. Each requires
    ref must have a directory row.
    downloads are upserted and settled download states deleted. With downloads_exist, a
    download or settled key that has no download state is dropped, with the open batch's
    directory rows for its target, instead of failing the commit.
    """

    items: Sequence[TrackedItem] = ()
    unchanged: Sequence[TrackedItem] = ()
    downloads: Sequence[DownloadState] = ()
    settled: Collection[DownloadKey] = frozenset()
    requires: Collection[DirectoryRef] = frozenset()
    downloads_exist: bool = False
    complete_teams: Collection[str] = frozenset()


@dataclass(frozen=True)
class Committed:
    """The revision each committed item now has, and the download keys the commit dropped."""

    revisions: dict[ItemKey, int]
    dropped: set[DownloadKey]


class ArchiveStore(Protocol):
    """Appends and drops wait in an open batch. Reads see the batch's appended lines; a drop
    removes matching appended lines at once and committed rows when the batch commits.

    commit writes the batch and the change in one transaction. When a check of the Commit
    fails, or a team with directory lines in the batch has no tracked item that is not
    removed (counting the commit's items), it writes nothing, empties the batch and raises
    CommitConflict. discard empties the batch.
    """

    def append(self, env: Envelope) -> None: ...
    def commit(self, change: Commit) -> Committed: ...
    def discard(self) -> None: ...
    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]: ...
    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]: ...
    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]: ...
    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]: ...
    def removed_items(self) -> dict[tuple[str, str, str, str], TrackedItem]: ...
    def downloads(self) -> dict[DownloadKey, DownloadState]: ...
    def append_run(self, run: RunRecord, slarc_version: str) -> None: ...
    def channels(self, team: str) -> list[str]: ...  # channels with stored message lines
    def message_lines(self, team: str, channel: str) -> Iterator[Envelope]: ...  # oldest first
    def drop_messages(self, team: str, channel: str, stamps: set[str]) -> int: ...
    def drop_directory_lines(self, team: str, kind: Kind, ids: set[str]) -> int: ...
    def drop_workspace(self, team: str) -> None: ...
    def drop_downloads(self, team: str, targets: set[str]) -> None: ...
    def blob_references(self) -> list[tuple[str, Kind, str, str]]: ...  # (team, kind, id, blob)
    def maintain(self, blobs: BlobStore, deleting: Callable[[], None] = ...) -> bool:
        """Compacts the archive, then, only if no other command has it open, calls deleting and
        deletes what no current row uses: old and orphaned data and every blob no directory row
        references.

        Returns whether the deletion ran. Raises ValueError if the open batch is not empty.
        """
        ...


class ArchiveLock(Protocol):
    """The archive's read and write locks, which every process takes in that order.

    The read lock is held shared while the archive is open, counted per process, or tried
    exclusively without blocking, which needs this process not to hold it shared. write()
    holds the write lock exclusively around each transaction, blocking until it is free.
    Holding the read lock shared while this process holds it exclusively, or the write lock
    while this process already holds it, raises RuntimeError instead of waiting for ever.
    """

    def hold_shared(self) -> None: ...
    def release_shared(self) -> None: ...
    def held_shared(self) -> bool: ...  # whether this process holds the read lock shared
    def try_exclusive(self) -> bool: ...
    def release_exclusive(self) -> None: ...
    def write(self) -> AbstractContextManager[None]: ...


# How many matches a search returns unless asked for more.
SEARCH_LIMIT = 50


@dataclass(frozen=True)
class SearchFilter:
    """What a search matches. Every field narrows it; text is matched in plain, ignoring case.

    in_channel is a conversation id or name, in_dm_user a user a DM is with. from_user and
    mentions are a user id, name, display name or real name, or "me", the user of each
    workspace's latest auth. limit 0 means no limit.
    """

    text: str | None = None
    in_channel: str | None = None
    in_dm_user: str | None = None
    from_user: str | None = None
    mentions: str | None = None
    window: Window = field(default_factory=Window)
    limit: int = 0


@dataclass(frozen=True)
class SearchHit:
    """A stored message matching a search, with message_readable's fields and a snippet of
    its text around the match."""

    team_id: str
    workspace: str
    channel_id: str
    conversation: str
    ts: str
    thread_ts: str | None
    posted_at: datetime
    author: str
    author_real_name: str
    text: str
    link: str
    deleted_at: str | None
    first_seen_at: str | None
    snippet: str


class QueryIndex(Protocol):
    def sql(self, query: str) -> tuple[list[str], list[tuple]]: ...
    def search(self, query: SearchFilter) -> list[SearchHit]: ...
    # Whether a conversation (id or name, or the DM with a user) or a user is known, as search
    # filters match them.
    def knows_conversation(self, channel: str | None, dm_user: str | None) -> bool: ...
    def knows_user(self, user: str) -> bool: ...
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


@dataclass(frozen=True)
class ItemProgress:
    """How far a pull has read one item: messages read and lines new for it. While history is
    read, at is the ts of the oldest message reached and target the ts paging reads back to.
    share is the part of the item read, from 0 to 1, or None while it is unknown; 1 when the
    item is finished."""

    read: int
    new: int
    at: str | None = None
    target: str | None = None
    share: float | None = None


class Reporter(Protocol):
    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def progress(self, label: str, update: ItemProgress) -> None: ...
    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None: ...
    def archive(self, step: str, *, finished: bool = False) -> None: ...  # saving the archive
