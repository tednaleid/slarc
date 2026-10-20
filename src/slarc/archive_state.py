# ABOUTME: Folds archive lines into current state, where the latest (fetched_at, run) line wins.
# ABOUTME: Shared by every ArchiveStore so duplicate and out-of-order lines read the same.
from collections.abc import Iterable
from dataclasses import replace

import msgspec

from slarc.domain.envelope import Envelope, Meta
from slarc.domain.tracking import DownloadState, RunRecord, TrackedItem, run_from_envelope
from slarc.ports import DirectoryState, StoredMessage


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


def _run_records(lines: Iterable[Envelope]) -> list[RunRecord]:
    """Run records oldest first; a run line whose object does not convert is skipped."""
    runs = sorted(
        (env for env in lines if env.meta.kind == "run"), key=lambda e: version_key(e.meta)
    )
    records = []
    for env in runs:
        try:
            records.append(run_from_envelope(env))
        except msgspec.ValidationError:
            continue
    return records


def _latest_tracked(lines: Iterable[Envelope]) -> dict[tuple[str, str, str, str], TrackedItem]:
    return {item.key: item for run in _run_records(lines) for item in run.tracked}


def tracked_items(lines: Iterable[Envelope]) -> dict[tuple[str, str, str, str], TrackedItem]:
    return {key: item for key, item in _latest_tracked(lines).items() if item.status != "removed"}


def removed_items(lines: Iterable[Envelope]) -> dict[tuple[str, str, str, str], TrackedItem]:
    return {key: item for key, item in _latest_tracked(lines).items() if item.status == "removed"}


def downloads(lines: Iterable[Envelope]) -> dict[tuple[str, str], DownloadState]:
    return {(d.team_id, d.target): d for run in _run_records(lines) for d in run.downloads}
