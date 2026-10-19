# ABOUTME: In-memory test doubles for slarc's ports: archive store, blob store and clock.
# ABOUTME: The archive fake keeps appended envelopes in order and reads them like the JSONL store.
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from slarc import archive_state
from slarc.archive_state import LatestMessages
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_envelope
from slarc.ports import DirectoryState, StoredMessage


class InMemoryArchiveStore:
    def __init__(self) -> None:
        self.lines: list[Envelope] = []

    def append(self, env: Envelope) -> None:
        self.lines.append(env)

    def flush(self) -> None:
        pass

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


class FakeClock:
    def __init__(self, current: datetime = datetime(2026, 10, 19, 13, 40, 11, tzinfo=UTC)) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
