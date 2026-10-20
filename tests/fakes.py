# ABOUTME: In-memory doubles for slarc's ports: archive, blobs, export files, Slack and credentials.
# ABOUTME: The archive fake reads like the JSONL store; the Slack fake pages and fails like Slack.
import hashlib
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from slarc import archive_state
from slarc.adapters.slack_gateway import MESSAGE_PAGE_LIMIT
from slarc.archive_state import LatestMessages
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.links import MessageTs
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_envelope
from slarc.ports import (
    BlobStore,
    CredentialReadError,
    DirectoryState,
    DownloadJob,
    DownloadResult,
    FileDeleted,
    NoSession,
    NotVisible,
    Session,
    SlarcError,
    StoredMessage,
    ThreadNotFound,
)


class InMemoryArchiveStore:
    def __init__(self) -> None:
        self.lines: list[Envelope] = []
        self.flushes = 0

    def append(self, env: Envelope) -> None:
        self.lines.append(env)

    def flush(self) -> None:
        self.flushes += 1

    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]:
        latest = LatestMessages()
        for env in self.lines:
            meta = env.meta
            if meta.kind in ("message", "deleted") and (meta.team, meta.channel) == (team, channel):
                latest.add(env)
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]:
        return archive_state.directory_state(self._directory_lines(team, kind))

    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]:
        return archive_state.directory_objects(self._directory_lines(team, kind))

    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]:
        return archive_state.tracked_items(self.lines)

    def downloads(self) -> dict[tuple[str, str], DownloadState]:
        return archive_state.downloads(self.lines)

    def append_run(self, run: RunRecord, slarc_version: str) -> None:
        self.append(run_envelope(run, slarc_version))

    def _directory_lines(self, team: str, kind: Kind) -> list[Envelope]:
        return [env for env in self.lines if (env.meta.team, env.meta.kind) == (team, kind)]


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
        for page in _pages(newest_first, self.page_size):
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


type FailedStatus = Literal["failed", "forbidden", "too_large", "deleted"]


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
        self.progress_updates: list[tuple[str, int, int, int | None]] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def progress(self, label: str, done: int, new: int, total: int | None) -> None:
        self.progress_updates.append((label, done, new, total))


class FakeAssetResolver:
    """Answers every asset with a stable local path built from its name, URL or file id."""

    def emoji_src(self, name: str) -> str | None:
        return f"assets/emoji/{name}"

    def image_src(self, url: str) -> str | None:
        return f"assets/images/{url.rsplit('/', 1)[-1].split('?')[0]}"

    def file_src(self, file_id: str) -> str | None:
        return f"assets/files/{file_id}"
