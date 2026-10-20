# ABOUTME: Folds archive lines into current state, where the latest (fetched_at, run) line wins,
# ABOUTME: and checks a commit's revisions. Shared by every ArchiveStore so they behave the same.
from collections.abc import Iterable, Mapping
from dataclasses import replace

import msgspec

from slarc.domain.envelope import Envelope, Meta
from slarc.domain.tracking import DownloadState, ItemKey, RunRecord, TrackedItem
from slarc.ports import Commit, CommitConflict, DirectoryState, DownloadKey, StoredMessage

type StoredRevisions = Mapping[ItemKey, tuple[int, str]]  # (revision, status) per tracked item

REMOVED_DOWNLOAD = "removed"


def version_key(meta: Meta) -> tuple[str, str]:
    return (meta.fetched_at or "", meta.run or "")


class LatestMessages:
    """The current StoredMessage per ts of one conversation, fed message and deleted lines."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[tuple[str, str], StoredMessage]] = {}

    def add(self, env: Envelope) -> None:
        meta = env.meta
        if meta.ts is None or meta.fetched_at is None:
            return
        key = version_key(meta)
        thread_ts = env.object.get("thread_ts") if env.object is not None else None
        held = self._held.get(meta.ts)
        if held is None or key >= held[0]:
            if meta.kind == "deleted" and held is not None:
                thread_ts = held[1].thread_ts
            stored = StoredMessage(
                hash=meta.hash if meta.kind == "message" else None,
                thread_ts=thread_ts,
                fetched_at=meta.fetched_at,
            )
            self._held[meta.ts] = (key, stored)
        elif held[1].hash is None and held[1].thread_ts is None and thread_ts is not None:
            # An older message line read after the delete still supplies the thread.
            self._held[meta.ts] = (held[0], replace(held[1], thread_ts=thread_ts))

    def current(self) -> dict[str, StoredMessage]:
        return {ts: stored for ts, (_, stored) in self._held.items()}


def latest_by_id(lines: Iterable[Envelope]) -> dict[str, Envelope]:
    """The latest directory line per meta.id; lines without an id or hash are skipped."""
    latest: dict[str, Envelope] = {}
    for env in lines:
        meta = env.meta
        if meta.id is None or meta.hash is None:
            continue
        held = latest.get(meta.id)
        if held is None or version_key(meta) >= version_key(held.meta):
            latest[meta.id] = env
    return latest


def directory_state(lines: Iterable[Envelope]) -> dict[str, DirectoryState]:
    return {
        id_: DirectoryState(hash=env.meta.hash or "", blob=env.meta.blob)
        for id_, env in latest_by_id(lines).items()
    }


def directory_objects(lines: Iterable[Envelope]) -> dict[str, dict]:
    return {id_: env.object or {} for id_, env in latest_by_id(lines).items()}


class _DownloadTarget(msgspec.Struct):
    team_id: str
    target: str


class LatestRuns:
    """The latest tracked item per key and download state per target, fed run lines in any order.

    A run line whose object does not convert is skipped. A download entry whose status is
    REMOVED_DOWNLOAD, which JSONL run lines use for a target whose state was deleted, leaves
    its target with no download state.
    """

    def __init__(self) -> None:
        self._tracked: dict[ItemKey, tuple[tuple[str, str], TrackedItem]] = {}
        self._downloads: dict[DownloadKey, tuple[tuple[str, str], DownloadState | None]] = {}

    def add(self, env: Envelope) -> None:
        if env.meta.kind != "run":
            return
        obj = dict(env.object or {})
        entries = obj.get("downloads")
        removed_entries = []
        if isinstance(entries, list):
            removed_entries = [entry for entry in entries if _is_removed(entry)]
            obj["downloads"] = [entry for entry in entries if not _is_removed(entry)]
        try:
            run = msgspec.convert(obj, RunRecord)
            removed = msgspec.convert(removed_entries, list[_DownloadTarget])
        except msgspec.ValidationError:
            return
        key = version_key(env.meta)
        for item in run.tracked:
            held = self._tracked.get(item.key)
            if held is None or key >= held[0]:
                self._tracked[item.key] = (key, item)
        states: list[tuple[DownloadKey, DownloadState | None]] = [
            ((state.team_id, state.target), state) for state in run.downloads
        ]
        states += [((entry.team_id, entry.target), None) for entry in removed]
        for target, state in states:
            held_state = self._downloads.get(target)
            if held_state is None or key >= held_state[0]:
                self._downloads[target] = (key, state)

    def tracked_items(self) -> dict[ItemKey, TrackedItem]:
        return {k: item for k, (_, item) in self._tracked.items() if item.status != "removed"}

    def removed_items(self) -> dict[ItemKey, TrackedItem]:
        return {k: item for k, (_, item) in self._tracked.items() if item.status == "removed"}

    def downloads(self) -> dict[DownloadKey, DownloadState]:
        return {
            target: state for target, (_, state) in self._downloads.items() if state is not None
        }


def _is_removed(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("status") == REMOVED_DOWNLOAD


def latest_runs(lines: Iterable[Envelope]) -> LatestRuns:
    latest = LatestRuns()
    for env in lines:
        latest.add(env)
    return latest


def item_revisions(change: Commit, stored: StoredRevisions) -> dict[ItemKey, int]:
    """The revision each of the commit's items is written with.

    Raises CommitConflict when an item or unchanged item no longer has the revision it was
    read with, or when an item with revision 0 is still tracked, and ValueError when two
    items share a key.
    """
    keys = [item.key for item in change.items]
    if len(set(keys)) != len(keys):
        raise ValueError("A commit names each tracked item at most once")
    revisions = {}
    for item in change.items:
        held = stored.get(item.key)
        if item.revision == 0:
            if held is not None and held[1] != "removed":
                raise CommitConflict()
            revisions[item.key] = (held[0] if held is not None else 0) + 1
        else:
            if held is None or held[0] != item.revision:
                raise CommitConflict()
            revisions[item.key] = item.revision + 1
    for item in change.unchanged:
        held = stored.get(item.key)
        if held is None or held[0] != item.revision:
            raise CommitConflict()
    return revisions


def dropped_downloads(change: Commit, existing: set[DownloadKey]) -> set[DownloadKey]:
    """With downloads_exist, the commit's download and settled keys that have no download state."""
    if not change.downloads_exist:
        return set()
    keys = {(state.team_id, state.target) for state in change.downloads} | set(change.settled)
    return keys - existing


def without_targets(batch: list[Envelope], targets: set[DownloadKey]) -> list[Envelope]:
    """The batch without the file and image lines of the targets."""
    return [
        env
        for env in batch
        if env.meta.kind not in ("file", "image") or (env.meta.team, env.meta.id) not in targets
    ]


def require_live_teams(
    batch: Iterable[Envelope], stored: StoredRevisions, items: Iterable[TrackedItem]
) -> None:
    """Raises CommitConflict when a directory line's team has no tracked item that is not
    removed, counting the commit's items."""
    statuses = {key: status for key, (_, status) in stored.items()}
    statuses.update((item.key, item.status) for item in items)
    live = {key[0] for key, status in statuses.items() if status != "removed"}
    teams = {env.meta.team for env in batch if env.meta.kind not in ("message", "deleted")}
    if not teams <= live:
        raise CommitConflict()
