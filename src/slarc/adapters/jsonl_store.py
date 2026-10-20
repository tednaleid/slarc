# ABOUTME: ArchiveStore over the archive's append-only JSONL files (raw/, directory/, runs.jsonl).
# ABOUTME: Keeps one open handle per file, flushed on flush(), and skips lines it cannot decode.
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

import msgspec

from slarc import archive_state
from slarc.archive_state import LatestMessages
from slarc.domain.envelope import Envelope, Kind, Meta, decode_line, encode_line, month_of
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_envelope
from slarc.ports import ArchiveWriteFailed, DirectoryState, StoredMessage

DIRECTORY_FILES: dict[str, str] = {
    "user": "users.jsonl",
    "emoji": "emoji.jsonl",
    "conversation": "conversations.jsonl",
    "bot": "bots.jsonl",
    "usergroup": "usergroups.jsonl",
    "file": "files.jsonl",
    "image": "images.jsonl",
}


class JsonlArchiveStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._handles: dict[Path, BinaryIO] = {}
        self._messages: dict[tuple[str, str], LatestMessages] = {}

    def append(self, env: Envelope) -> None:
        meta = env.meta
        self._handle(self._path_of(meta)).write(encode_line(env))
        if meta.kind in ("message", "deleted"):
            cached = self._messages.get((meta.team or "", meta.channel or ""))
            if cached is not None:
                cached.add(env)

    def flush(self) -> None:
        for handle in self._handles.values():
            handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()

    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]:
        latest = self._messages.get((team, channel))
        if latest is None:
            latest = LatestMessages()
            months = sorted((self._root / "raw" / team / channel).glob("*.jsonl"))
            for env in self._read(months):
                if env.meta.kind in ("message", "deleted"):
                    latest.add(env)
            self._messages[(team, channel)] = latest
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]:
        return archive_state.directory_state(self._read([self._directory_path(team, kind)]))

    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]:
        return archive_state.directory_objects(self._read([self._directory_path(team, kind)]))

    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]:
        return archive_state.tracked_items(self._read([self._runs_path()]))

    def removed_items(self) -> dict[tuple[str, str, str, str], TrackedItem]:
        return archive_state.removed_items(self._read([self._runs_path()]))

    def downloads(self) -> dict[tuple[str, str], DownloadState]:
        return archive_state.downloads(self._read([self._runs_path()]))

    def append_run(self, run: RunRecord, slarc_version: str) -> None:
        self.append(run_envelope(run, slarc_version))
        self.flush()

    def _path_of(self, meta: Meta) -> Path:
        if meta.kind == "run":
            return self._runs_path()
        if meta.team is None:
            raise ValueError(f"A {meta.kind} line needs meta.team")
        if meta.kind in ("message", "deleted"):
            if meta.channel is None or meta.ts is None:
                raise ValueError(f"A {meta.kind} line needs meta.channel and meta.ts")
            month = month_of(meta.ts)
            return self._root / "raw" / meta.team / meta.channel / f"{month}.jsonl"
        return self._directory_path(meta.team, meta.kind)

    def _directory_path(self, team: str, kind: Kind) -> Path:
        return self._root / "directory" / team / DIRECTORY_FILES[kind]

    def _runs_path(self) -> Path:
        return self._root / "runs.jsonl"

    def _handle(self, path: Path) -> BinaryIO:
        handle = self._handles.get(path)
        if handle is None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
            except OSError as e:
                raise ArchiveWriteFailed(path, e.strerror) from None
            # A crash can leave a partial last line; start the next line after it.
            if handle.seek(0, os.SEEK_END) > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            self._handles[path] = handle
        return handle

    def _read(self, paths: Iterable[Path]) -> Iterator[Envelope]:
        """Envelopes from each existing file; truncated lines and unknown kinds are skipped."""
        self.flush()
        for path in paths:
            if not path.exists():
                continue
            with path.open("rb") as lines:
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        yield decode_line(line)
                    except msgspec.DecodeError:
                        continue
