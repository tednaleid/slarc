# ABOUTME: ArchiveStore over the archive's JSONL files (raw/, directory/, runs.jsonl), appended to
# ABOUTME: through one open handle per file; only the drop methods rewrite or delete a file.
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

import msgspec

from slarc import archive_state
from slarc.adapters.jsonl_import import DIRECTORY_FILES
from slarc.archive_state import LatestMessages, LatestRuns
from slarc.domain.envelope import Envelope, Kind, Meta, decode_line, encode_line, month_of
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_envelope
from slarc.ports import ArchiveWriteFailed, DirectoryState, StoredMessage


class JsonlArchiveStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._handles: dict[Path, BinaryIO] = {}
        self._messages: dict[tuple[str, str], LatestMessages] = {}
        self._runs: LatestRuns | None = None

    def append(self, env: Envelope) -> None:
        meta = env.meta
        self._handle(self._path_of(meta)).write(encode_line(env))
        if meta.kind in ("message", "deleted"):
            cached = self._messages.get((meta.team or "", meta.channel or ""))
            if cached is not None:
                cached.add(env)
        elif meta.kind == "run" and self._runs is not None:
            self._runs.add(env)

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
            for env in self._read(self._months(team, channel)):
                if env.meta.kind in ("message", "deleted"):
                    latest.add(env)
            self._messages[(team, channel)] = latest
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]:
        return archive_state.directory_state(self._read([self._directory_path(team, kind)]))

    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]:
        return archive_state.directory_objects(self._read([self._directory_path(team, kind)]))

    def tracked_items(self) -> dict[tuple[str, str, str, str], TrackedItem]:
        return self._latest_runs().tracked_items()

    def removed_items(self) -> dict[tuple[str, str, str, str], TrackedItem]:
        return self._latest_runs().removed_items()

    def downloads(self) -> dict[tuple[str, str], DownloadState]:
        return self._latest_runs().downloads()

    def append_run(self, run: RunRecord, slarc_version: str) -> None:
        """Appends the run line and syncs it to disk, after syncing every line appended before it.

        A power loss then never keeps a run line whose cursors point past lost message lines,
        and rm's sweep never deletes anything before the removal it applies is on disk.
        """
        self._sync()
        self.append(run_envelope(run, slarc_version))
        self._sync()

    def channels(self, team: str) -> list[str]:
        raw = self._root / "raw" / team
        if not raw.exists():
            return []
        return sorted(d.name for d in raw.iterdir() if any(d.glob("*.jsonl")))

    def message_lines(self, team: str, channel: str) -> Iterator[Envelope]:
        for env in self._read(self._months(team, channel)):
            if env.meta.kind in ("message", "deleted"):
                yield env

    def drop_messages(self, team: str, channel: str, stamps: set[str]) -> int:
        self._messages.pop((team, channel), None)
        dropped = sum(
            self._rewrite(month, lambda meta: meta.ts in stamps)
            for month in self._months(team, channel)
        )
        channel_dir = self._root / "raw" / team / channel
        if channel_dir.exists() and not any(channel_dir.iterdir()):
            channel_dir.rmdir()
        return dropped

    def drop_directory_lines(self, team: str, kind: Kind, ids: set[str]) -> int:
        return self._rewrite(
            self._directory_path(team, kind), lambda meta: meta.kind == kind and meta.id in ids
        )

    def drop_workspace(self, team: str) -> None:
        self._messages = {key: v for key, v in self._messages.items() if key[0] != team}
        for top in (self._root / "raw" / team, self._root / "directory" / team):
            for path in [path for path in self._handles if path.is_relative_to(top)]:
                self._handles.pop(path).close()
            if not top.exists():
                continue
            try:
                shutil.rmtree(top)
            except OSError as e:
                raise ArchiveWriteFailed(top, e.strerror) from None

    def blob_references(self) -> list[tuple[str, Kind, str, str]]:
        references = []
        for team_dir in sorted((self._root / "directory").glob("*")):
            for kind, name in DIRECTORY_FILES.items():
                for env in self._read([team_dir / name]):
                    if env.meta.kind == kind and env.meta.id and env.meta.blob:
                        references.append((team_dir.name, kind, env.meta.id, env.meta.blob))
        return list(dict.fromkeys(references))

    def _latest_runs(self) -> LatestRuns:
        """The folded run lines, read from runs.jsonl once and then kept current by append."""
        if self._runs is None:
            self._runs = archive_state.latest_runs(self._read([self._runs_path()]))
        return self._runs

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

    def _months(self, team: str, channel: str) -> list[Path]:
        return sorted((self._root / "raw" / team / channel).glob("*.jsonl"))

    def _sync(self) -> None:
        for handle in self._handles.values():
            handle.flush()
            os.fsync(handle.fileno())

    def _rewrite(self, path: Path, drop: Callable[[Meta], bool]) -> int:
        """Removes the lines whose meta matches drop and returns how many it removed.

        The kept lines go to a temp file that replaces the original, so an interrupt leaves
        one or the other. A file left with no lines is deleted. Lines that do not decode stay.
        """
        handle = self._handles.pop(path, None)
        if handle is not None:
            handle.close()
        if not path.exists():
            return 0
        kept: list[bytes] = []
        dropped = 0
        with path.open("rb") as lines:
            for line in lines:
                if not line.strip():
                    continue
                try:
                    meta = decode_line(line).meta
                except msgspec.DecodeError:
                    meta = None
                if meta is not None and drop(meta):
                    dropped += 1
                else:
                    kept.append(line)
        if dropped == 0:
            return 0
        tmp_name = None
        try:
            if not kept:
                path.unlink()
                return dropped
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as tmp:
                tmp_name = tmp.name
                tmp.writelines(kept)
                tmp.flush()
                os.fsync(tmp.fileno())
            shutil.copymode(path, tmp_name)
            os.replace(tmp_name, path)
        except BaseException as e:
            if tmp_name is not None:
                Path(tmp_name).unlink(missing_ok=True)
            if isinstance(e, OSError):
                raise ArchiveWriteFailed(path, e.strerror) from None
            raise
        return dropped

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
