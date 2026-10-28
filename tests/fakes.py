# ABOUTME: In-memory doubles for slarc's ports: archive, lock, blobs, exports, Slack, credentials.
# ABOUTME: The archive fake commits like the lake store; the Slack fake pages and fails like Slack.
import hashlib
from collections import Counter, deque
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import msgspec

from slarc import archive_state
from slarc.adapters.slack_gateway import MESSAGE_PAGE_LIMIT
from slarc.archive_state import LatestMessages
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.links import MessageTs
from slarc.domain.tracking import DownloadState, ItemKey, RunRecord, TrackedItem, run_envelope
from slarc.ports import (
    BlobStore,
    Commit,
    CommitConflict,
    Committed,
    ConnectionLost,
    CredentialReadError,
    DirectoryState,
    DownloadJob,
    DownloadKey,
    DownloadResult,
    FileDeleted,
    ItemProgress,
    NoSession,
    NotVisible,
    Session,
    SlarcError,
    StoredMessage,
    ThreadNotFound,
)

MESSAGE_KINDS = ("message", "deleted")


class InMemoryArchiveStore:
    """Committed lines, run lines included, with tracked item and download state tables.

    Appends and drops wait in an open batch that commit applies with the lake store's checks.
    maintain deletes unreferenced blobs while idle is true, as if no other command were open.
    """

    def __init__(self) -> None:
        self.idle = True
        self.lines: list[Envelope] = []
        self.tracked: dict[ItemKey, TrackedItem] = {}
        self.download_states: dict[DownloadKey, DownloadState] = {}
        self._snapshot = 0
        self._batch: list[Envelope] = []
        self._line_drops: list[Callable[[Envelope], bool]] = []
        self._download_drops: set[DownloadKey] = set()

    def append(self, env: Envelope) -> None:
        self._batch.append(env)

    def commit(self, change: Commit) -> Committed:
        try:
            committed = self._commit(change)
        except CommitConflict:
            self.discard()
            raise
        self.discard()
        self._snapshot += 1
        return committed

    def discard(self) -> None:
        self._batch.clear()
        self._line_drops.clear()
        self._download_drops.clear()

    @contextmanager
    def elsewhere(self) -> Generator[InMemoryArchiveStore]:
        """Yields the store with an empty open batch, as another command would see it, and puts
        this command's open batch back afterwards."""
        held = (self._batch, self._line_drops, self._download_drops)
        self._batch, self._line_drops, self._download_drops = [], [], set()
        try:
            yield self
        finally:
            self._batch, self._line_drops, self._download_drops = held

    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]:
        latest = LatestMessages()
        for env in self._team_message_lines(team):
            if env.meta.channel == channel:
                latest.add(env)
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]:
        return archive_state.directory_state(self._directory_lines(team, kind))

    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]:
        return archive_state.directory_objects(self._directory_lines(team, kind))

    def tracked_items(self) -> dict[ItemKey, TrackedItem]:
        return {key: item for key, item in self.tracked.items() if item.status != "removed"}

    def removed_items(self) -> dict[ItemKey, TrackedItem]:
        return {key: item for key, item in self.tracked.items() if item.status == "removed"}

    def downloads(self) -> dict[DownloadKey, DownloadState]:
        return dict(self.download_states)

    def append_run(self, run: RunRecord, slarc_version: str) -> None:
        """Records the run line with tracked and downloads emptied; raises ValueError if the
        open batch is not empty."""
        if self._batch or self._line_drops or self._download_drops:
            raise ValueError("Commit or discard the open batch before appending the run")
        recorded = msgspec.structs.replace(run, tracked=[], downloads=[])
        self.lines.append(run_envelope(recorded, slarc_version))
        self._snapshot += 1

    def snapshot(self) -> int:
        return self._snapshot

    def directory_listed_at(self, team: str) -> str | None:
        listed = [
            entry["directory_listed_at"]
            for env in self.lines
            if env.meta.kind == "run"
            for entry in (env.object or {}).get("auth") or []
            if entry.get("team_id") == team and entry.get("directory_listed_at")
        ]
        return max(listed, default=None)

    def channels(self, team: str) -> list[str]:
        return sorted({env.meta.channel or "" for env in self._team_message_lines(team)})

    def message_lines(self, team: str, channel: str) -> Iterator[Envelope]:
        return iter([env for env in self._team_message_lines(team) if env.meta.channel == channel])

    def drop_messages(self, team: str, channel: str, stamps: set[str]) -> int:
        return self._stage_drop(
            lambda env: (
                env.meta.kind in MESSAGE_KINDS
                and (env.meta.team, env.meta.channel) == (team, channel)
                and env.meta.ts in stamps
            )
        )

    def drop_directory_lines(self, team: str, kind: Kind, ids: set[str]) -> int:
        return self._stage_drop(
            lambda env: (env.meta.team, env.meta.kind) == (team, kind) and env.meta.id in ids
        )

    def drop_workspace(self, team: str) -> None:
        self._stage_drop(lambda env: env.meta.team == team and env.meta.kind != "run")

    def drop_downloads(self, team: str, targets: set[str]) -> None:
        self._download_drops |= {(team, target) for target in targets}

    def blob_references(self) -> list[tuple[str, Kind, str, str]]:
        references = [
            (env.meta.team or "", env.meta.kind, env.meta.id or "", env.meta.blob)
            for env in self._visible()
            if env.meta.blob is not None
        ]
        return list(dict.fromkeys(references))

    def maintain(self, blobs: BlobStore, deleting: Callable[[], None] = lambda: None) -> bool:
        if self._batch or self._line_drops or self._download_drops:
            raise ValueError("Commit or discard the open batch before maintaining the archive")
        if not self.idle:
            return False
        deleting()
        blobs.delete_partial()
        referenced = {blob for _, _, _, blob in self.blob_references()}
        for blob in sorted(set(blobs.ids()) - referenced):
            blobs.delete(blob)
        return True

    def _commit(self, change: Commit) -> Committed:
        stored = self._stored_revisions()
        revisions = archive_state.item_revisions(change, stored)
        archive_state.require_complete_teams(change, stored)
        dropped = archive_state.dropped_downloads(change, set(self.download_states))
        batch = archive_state.without_targets(self._batch, dropped)
        archive_state.require_live_teams(batch, stored, change.items)
        lines = [env for env in self.lines if not self._staged(env)] + batch
        directory = {
            (env.meta.team, env.meta.kind, env.meta.id)
            for env in lines
            if env.meta.kind not in (*MESSAGE_KINDS, "run")
        }
        if not set(change.requires) <= directory:
            raise CommitConflict()
        self.lines = lines
        for key in self._download_drops:
            self.download_states.pop(key, None)
        for item in change.items:
            self.tracked[item.key] = msgspec.structs.replace(item, revision=revisions[item.key])
        for state in change.downloads:
            if (state.team_id, state.target) not in dropped:
                self.download_states[(state.team_id, state.target)] = state
        for key in change.settled:
            self.download_states.pop(key, None)
        return Committed(revisions=revisions, dropped=dropped)

    def _stored_revisions(self) -> archive_state.StoredRevisions:
        return {key: (item.revision, item.status) for key, item in self.tracked.items()}

    def _stage_drop(self, drop: Callable[[Envelope], bool]) -> int:
        """Drops matching batch lines now and stages the committed ones; returns both counts."""
        kept = [env for env in self._batch if not drop(env)]
        dropped = len(self._batch) - len(kept)
        self._batch = kept
        self._line_drops.append(drop)
        return dropped + sum(1 for env in self.lines if drop(env))

    def _staged(self, env: Envelope) -> bool:
        return any(drop(env) for drop in self._line_drops)

    def _visible(self) -> list[Envelope]:
        return self.lines + self._batch

    def _directory_lines(self, team: str, kind: Kind) -> list[Envelope]:
        return [env for env in self._visible() if (env.meta.team, env.meta.kind) == (team, kind)]

    def _team_message_lines(self, team: str) -> list[Envelope]:
        return [
            env
            for env in self._visible()
            if env.meta.kind in MESSAGE_KINDS and env.meta.team == team
        ]


class FakeArchiveLock:
    """Records each lock call in events, a write as "write" and "end write"; try_exclusive
    succeeds while exclusive_free is true. Raises RuntimeError where FlockArchiveLock does."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.exclusive_free = True
        self.shared = 0
        self.exclusive = False
        self.writing = False

    def hold_shared(self) -> None:
        if self.exclusive:
            raise RuntimeError("Release the exclusive read lock before holding it shared")
        self.events.append("hold_shared")
        self.shared += 1

    def release_shared(self) -> None:
        if not self.shared:
            raise RuntimeError("The read lock is not held shared")
        self.events.append("release_shared")
        self.shared -= 1

    def held_shared(self) -> bool:
        return self.shared > 0

    def try_exclusive(self) -> bool:
        if self.shared or self.exclusive:
            raise RuntimeError("Release the read lock before trying it exclusively")
        self.events.append("try_exclusive")
        self.exclusive = self.exclusive_free
        return self.exclusive

    def release_exclusive(self) -> None:
        if not self.exclusive:
            raise RuntimeError("The read lock is not held exclusively")
        self.events.append("release_exclusive")
        self.exclusive = False

    @contextmanager
    def write(self) -> Generator[None]:
        if self.writing:
            raise RuntimeError("The write lock is already held by this process")
        self.events.append("write")
        self.writing = True
        try:
            yield
        finally:
            self.writing = False
            self.events.append("end write")


class InMemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def write(self, chunks: Iterable[bytes]) -> tuple[str, int]:
        data = b"".join(chunks)
        sha256 = hashlib.sha256(data).hexdigest()
        self.blobs[sha256] = data
        return sha256, len(data)

    def exists(self, sha256: str) -> bool:
        return sha256 in self.blobs

    def read(self, sha256: str) -> bytes:
        return self.blobs[sha256]

    def ids(self) -> Iterator[str]:
        return iter(list(self.blobs))

    def delete(self, sha256: str) -> None:
        self.blobs.pop(sha256, None)

    def delete_partial(self) -> None:
        pass


class InMemoryExportWriter:
    def __init__(self) -> None:
        self.files: dict[Path, bytes] = {}

    def write(self, path: Path, data: bytes) -> None:
        self.files[path] = data

    def text(self, path: Path) -> str:
        return self.files[path].decode("utf-8")


class FakeClock:
    def __init__(self, current: datetime = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


SPIKE_AUTH = {
    "ok": True,
    "url": "https://naleidworkspace.slack.com/",
    "team": "naleid",
    "user": "contact",
    "team_id": "T0CB5LYPVFA",
    "user_id": "U0CA538UEN7",
    "is_enterprise_install": False,
}


def _pages[T](items: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
    if not items:
        yield []


class FakeSlackGateway:
    """Serves canned Slack data with the gateway's page sizes and errors, recording each call."""

    def __init__(
        self,
        conversations: dict[str, list[dict]] | None = None,
        *,
        infos: dict[str, dict] | None = None,
        users: list[dict] | None = None,
        emoji: dict[str, str] | None = None,
        usergroups: list[dict] | None = None,
        bots: dict[str, dict] | None = None,
        files: dict[str, dict] | None = None,
        deleted_files: Iterable[str] = (),
        search_results: dict[str, list[dict]] | None = None,
        auth: dict | None = None,
        auth_error: SlarcError | None = None,
        page_size: int = MESSAGE_PAGE_LIMIT,
    ) -> None:
        self.conversations = conversations or {}
        self.infos = infos or {}
        self.users_list = users or []
        self.emoji_map = emoji or {}
        self.usergroups_list = usergroups or []
        self.bots = bots or {}
        self.files = files or {}
        self.deleted_files = set(deleted_files)
        self.search_results = search_results or {}
        self.auth = SPIKE_AUTH if auth is None else auth
        self.auth_error = auth_error
        self.page_size = page_size
        self.calls: list[tuple[str | None, ...]] = []
        self.rate_limit_wait_s = 0.0
        self._authenticated = False
        self._history_hooks: dict[int, Callable[[], None]] = {}

    @property
    def api_calls(self) -> dict[str, int]:
        return dict(Counter(str(call[0]) for call in self.calls))

    def auth_test(self) -> dict:
        """Like the gateway, calls auth.test only until it first succeeds."""
        if self.auth_error is not None or not self._authenticated:
            self.calls.append(("auth.test",))
        if self.auth_error is not None:
            raise self.auth_error
        self._authenticated = True
        return dict(self.auth)

    def conversation_info(self, channel: str) -> dict:
        self.calls.append(("conversations.info", channel))
        self._require_visible(channel)
        return self.infos.get(channel, {"id": channel})

    def list_conversations(self) -> Iterator[dict]:
        self.calls.append(("conversations.list",))
        for channel in self.conversations:
            yield self.infos.get(channel, {"id": channel})

    def before_history_page(self, number: int, callback: Callable[[], None]) -> None:
        """Runs callback once, the first time a history call is about to serve its page number,
        counting from 1."""
        self._history_hooks[number] = callback

    def history(self, channel: str, oldest: str | None, latest: str | None) -> Iterator[list[dict]]:
        self._require_visible(channel)

        def in_history(m: dict) -> bool:
            top_level = m.get("thread_ts") in (None, m["ts"])
            return top_level or m.get("subtype") == "thread_broadcast"

        def in_range(m: dict) -> bool:
            ts = MessageTs(m["ts"])
            return (oldest is None or ts > oldest) and (latest is None or ts < latest)

        selected = [m for m in self.conversations[channel] if in_history(m) and in_range(m)]
        newest_first = sorted(selected, key=lambda m: MessageTs(m["ts"]), reverse=True)
        for number, page in enumerate(_pages(newest_first, self.page_size), start=1):
            hook = self._history_hooks.pop(number, None)
            if hook is not None:
                hook()
            self.calls.append(("conversations.history", channel, oldest, latest))
            yield page

    def replies(self, channel: str, ts: str) -> list[dict]:
        self.calls.append(("conversations.replies", channel, ts))
        self._require_visible(channel)
        messages = self.conversations[channel]
        if not any(m["ts"] == ts for m in messages):
            raise ThreadNotFound(channel, ts)
        thread = [m for m in messages if m["ts"] == ts or m.get("thread_ts") == ts]
        return sorted(thread, key=lambda m: MessageTs(m["ts"]))

    def search(self, query: str) -> Iterator[dict]:
        for page in _pages(self.search_results.get(query, []), 100):
            self.calls.append(("search.messages", query))
            yield from page

    def users(self) -> Iterator[dict]:
        self.calls.append(("users.list",))
        yield from self.users_list

    def usergroups(self) -> list[dict]:
        self.calls.append(("usergroups.list",))
        return list(self.usergroups_list)

    def emoji(self) -> dict[str, str]:
        self.calls.append(("emoji.list",))
        return dict(self.emoji_map)

    def bot_info(self, bot_id: str) -> dict:
        self.calls.append(("bots.info", bot_id))
        if bot_id not in self.bots:
            raise SlarcError("Slack returned bot_not_found for bots.info.")
        return self.bots[bot_id]

    def rtm_url(self) -> str:
        self.calls.append(("rtm.connect",))
        return "wss://fake.invalid/rtm"

    def user_info(self, user_id: str) -> dict:
        self.calls.append(("users.info", user_id))
        user = next((u for u in self.users_list if u.get("id") == user_id), None)
        if user is None:
            raise SlarcError("Slack returned user_not_found for users.info.")
        return user

    def file_info(self, file_id: str) -> dict:
        self.calls.append(("files.info", file_id))
        if file_id in self.deleted_files:
            raise FileDeleted(file_id)
        if file_id not in self.files:
            raise SlarcError("Slack returned file_not_found for files.info.")
        return self.files[file_id]

    def _require_visible(self, channel: str) -> None:
        if channel not in self.conversations:
            raise NotVisible(channel)


class FakeConnector:
    """Hands out a fake gateway per session, choosing sessions the way SessionConnector does."""

    def __init__(self, gateways: dict[Session, FakeSlackGateway]) -> None:
        self.gateways = gateways

    def sessions(self) -> list[Session]:
        return list(self.gateways)

    def connect(self, workspace: str) -> FakeSlackGateway:
        for session, gateway in self.gateways.items():
            if session.matches(workspace):
                gateway.auth_test()
                return gateway
        raise NoSession(workspace)


type FailedStatus = Literal["failed", "forbidden", "not_found", "too_large", "deleted", "not_tried"]


class FakeDownloader:
    """Writes the bytes served for a URL into the blob store, recording each job it is handed.

    A URL with nothing served yields no result, as if the run stopped before reaching it.
    """

    def __init__(self, blobs: BlobStore | None = None) -> None:
        self.blobs = blobs if blobs is not None else InMemoryBlobStore()
        self.jobs: list[DownloadJob] = []
        self.parallelism: list[int] = []
        self._served: dict[str, tuple[bytes, str]] = {}
        self._failures: dict[str, tuple[FailedStatus, str]] = {}

    def serve(self, url: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._failures.pop(url, None)
        self._served[url] = (data, content_type)

    def fail(self, url: str, status: FailedStatus = "failed", error: str = "HTTP 500") -> None:
        self._served.pop(url, None)
        self._failures[url] = (status, error)

    def fetch(self, jobs: list[DownloadJob], parallelism: int) -> Iterator[DownloadResult]:
        self.jobs.extend(jobs)
        self.parallelism.append(parallelism)
        results = (self._result(job) for job in jobs)
        return iter([result for result in results if result is not None])

    def _result(self, job: DownloadJob) -> DownloadResult | None:
        if job.url in self._failures:
            status, error = self._failures[job.url]
            return DownloadResult(job, status, None, None, None, error)
        if job.url not in self._served:
            return None
        data, content_type = self._served[job.url]
        if len(data) > job.max_bytes:
            return DownloadResult(job, "too_large", None, None, None, "Larger than the limit.")
        sha256, size = self.blobs.write([data])
        return DownloadResult(job, "ok", sha256, size, content_type, None)


class FakeCredentialSource:
    def __init__(
        self, sessions: list[Session] | None = None, error: CredentialReadError | None = None
    ) -> None:
        self._sessions = sessions or []
        self._error = error

    def sessions(self) -> list[Session]:
        if self._error is not None:
            raise self._error
        return list(self._sessions)


class FakeKeyring:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.entries.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.entries[(service_name, username)] = password


class RecordingReporter:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.progress_updates: list[tuple[str, ItemProgress]] = []
        self.download_updates: list[tuple[int, int, int, int]] = []
        self.archive_steps: list[tuple[str, bool]] = []
        self.pulls: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def progress(self, label: str, update: ItemProgress) -> None:
        self.progress_updates.append((label, update))

    def downloads(self, done: int, failed: int, skipped: int, total: int) -> None:
        self.download_updates.append((done, failed, skipped, total))

    def archive(self, step: str, *, finished: bool = False) -> None:
        self.archive_steps.append((step, finished))

    def pull_started(self) -> None:
        self.pulls.append("started")

    def pull_ended(self) -> None:
        self.pulls.append("ended")


class FakeAssetResolver:
    """Answers every asset with a stable local path built from its name, URL or file id."""

    def emoji_src(self, name: str) -> str | None:
        return f"assets/emoji/{name}"

    def image_src(self, url: str) -> str | None:
        return f"assets/images/{url.rsplit('/', 1)[-1].split('?')[0]}"

    def file_src(self, file_id: str) -> str | None:
        return f"assets/files/{file_id}"


class FakeEventConnection:
    """Serves queued events in order; an exception queued is raised from receive instead, and a
    None reads as no event. Each ping is answered with a pong unless answers_pings is false."""

    def __init__(
        self, events: Iterable[dict | Exception | None] = (), answers_pings: bool = True
    ) -> None:
        self.events: deque[dict | Exception | None] = deque(events)
        self.answers_pings = answers_pings
        self.pings = 0
        self.closed = False

    def receive(self, timeout: float) -> dict | None:
        if self.closed:
            raise ConnectionLost("The event connection closed.")
        if not self.events:
            return None
        event = self.events.popleft()
        if isinstance(event, Exception):
            raise event
        return event

    def ping(self) -> None:
        if self.closed:
            raise ConnectionLost("The event connection closed.")
        self.pings += 1
        if self.answers_pings:
            self.events.append({"type": "pong", "reply_to": self.pings})

    def close(self) -> None:
        self.closed = True


class FakeEventConnector:
    """Opens the next connection queued for a workspace, or a quiet new one; an exception queued
    is raised instead. Records each workspace opened, in order."""

    def __init__(
        self, queued: dict[str, list[FakeEventConnection | Exception]] | None = None
    ) -> None:
        self.queued = {name: deque(items) for name, items in (queued or {}).items()}
        self.opened: list[str] = []
        self.connections: list[FakeEventConnection] = []

    def open(self, workspace: str) -> FakeEventConnection:
        self.opened.append(workspace)
        queue = self.queued.get(workspace)
        connection = queue.popleft() if queue else FakeEventConnection()
        if isinstance(connection, Exception):
            raise connection
        self.connections.append(connection)
        return connection
