# ABOUTME: ArchiveStore over the archive's DuckLake. Appends and drops wait in an open batch that
# ABOUTME: commit writes in one transaction, under the write lock, after checking item revisions.
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import msgspec

from slarc import archive_state
from slarc.adapters.lake import connect_lake
from slarc.archive_state import LatestMessages
from slarc.domain.envelope import Envelope, Kind, Meta
from slarc.domain.tracking import DownloadState, ItemKey, RunRecord, TrackedItem, run_envelope
from slarc.ports import (
    ArchiveLock,
    Commit,
    CommitConflict,
    Committed,
    DirectoryRef,
    DirectoryState,
    DownloadKey,
    StoredMessage,
)

MESSAGE_KINDS = ("message", "deleted")

MESSAGE_COLUMNS = {
    "team": "VARCHAR",
    "channel": "VARCHAR",
    "ts": "VARCHAR",
    "thread_ts": "VARCHAR",
    "user": "VARCHAR",
    "kind": "VARCHAR",
    "fetched_at": "VARCHAR",
    "run": "VARCHAR",
    "source_team": "VARCHAR",
    "api": "VARCHAR",
    "slarc": "VARCHAR",
    "hash": "VARCHAR",
    "plain": "VARCHAR",
    "object": "JSON",
}
DIRECTORY_COLUMNS = {
    "team": "VARCHAR",
    "kind": "VARCHAR",
    "id": "VARCHAR",
    "fetched_at": "VARCHAR",
    "run": "VARCHAR",
    "api": "VARCHAR",
    "slarc": "VARCHAR",
    "hash": "VARCHAR",
    "blob": "VARCHAR",
    "object": "JSON",
}
RUN_COLUMNS = {
    "run": "VARCHAR",
    "command": "VARCHAR",
    "started": "VARCHAR",
    "finished": "VARCHAR",
    "fetched_at": "VARCHAR",
    "slarc": "VARCHAR",
    "object": "JSON",
}
TRACKED_COLUMNS = {
    "team": "VARCHAR",
    "kind": "VARCHAR",
    "channel": "VARCHAR",
    "ts": "VARCHAR",
    "revision": "INTEGER",
    "since": "VARCHAR",
    "until": "VARCHAR",
    "newest_ts": "VARCHAR",
    "oldest_ts": "VARCHAR",
    "backfill_complete": "BOOLEAN",
    "last_search_at": "VARCHAR",
    "last_pull_at": "VARCHAR",
    "status": "VARCHAR",
    "archived": "BOOLEAN",
    "last_read": "JSON",
    "retry_threads": "JSON",
}
DOWNLOAD_COLUMNS = {
    "team": "VARCHAR",
    "target_kind": "VARCHAR",
    "target": "VARCHAR",
    "url": "VARCHAR",
    "status": "VARCHAR",
    "attempts": "INTEGER",
    "last_error": "VARCHAR",
}
TRACKED_KEY = ("team", "kind", "channel", "ts")
DOWNLOAD_KEY = ("team", "target")
DIRECTORY_REF = ("team", "kind", "id")

# Ascending (fetched_at, run), then insertion order, which is the order archive_state's folds
# expect: of two lines with the same version, the later one wins.
VERSION_ORDER = "coalesce(fetched_at, ''), coalesce(run, ''), rowid"

_decode = msgspec.json.Decoder()


class DuckLakeArchiveStore:
    """Holds the archive's read lock from open to close, and the write lock for each write."""

    def __init__(self, root: Path, ext_dir: Path, lock: ArchiveLock) -> None:
        self._lock = lock
        lock.hold_shared()
        try:
            with lock.write():
                self._con = connect_lake(root, ext_dir)
        except BaseException:
            lock.release_shared()
            raise
        self._batch: list[Envelope] = []
        self._drops: list[tuple[str, str, list]] = []  # (table, condition, params) per DELETE

    def append(self, env: Envelope) -> None:
        meta = env.meta
        if meta.kind == "run":
            raise ValueError("A run is stored through append_run")
        if meta.team is None:
            raise ValueError(f"A {meta.kind} line needs meta.team")
        if meta.kind in MESSAGE_KINDS and (meta.channel is None or meta.ts is None):
            raise ValueError(f"A {meta.kind} line needs meta.channel and meta.ts")
        self._batch.append(env)

    def commit(self, change: Commit) -> Committed:
        """Writes the open batch and the change in one transaction, then empties the batch.

        On CommitConflict nothing is written and the batch is emptied; on any other failure
        nothing is written and the batch is kept.
        """
        try:
            with self._lock.write(), self._transaction():
                committed = self._commit(change)
        except CommitConflict:
            self.discard()
            raise
        self.discard()
        return committed

    def discard(self) -> None:
        self._batch.clear()
        self._drops.clear()

    def close(self) -> None:
        try:
            self._con.close()
        finally:
            self._lock.release_shared()

    def stored_messages(self, team: str, channel: str) -> dict[str, StoredMessage]:
        latest = LatestMessages()
        rows = self._con.execute(
            "SELECT kind, ts, fetched_at, run, hash, thread_ts FROM message_version "
            f"WHERE team = ? AND channel = ? ORDER BY {VERSION_ORDER}",
            [team, channel],
        ).fetchall()
        for kind, ts, fetched, run, hash_, thread_ts in rows:
            meta = Meta(kind=kind, ts=ts, fetched_at=fetched, run=run, hash=hash_)
            latest.add(Envelope(meta=meta, object={"thread_ts": thread_ts} if thread_ts else None))
        for env in self._batch_messages(team, channel):
            latest.add(env)
        return latest.current()

    def directory_state(self, team: str, kind: Kind) -> dict[str, DirectoryState]:
        return archive_state.directory_state(self._directory_lines(team, kind, with_object=False))

    def directory_objects(self, team: str, kind: Kind) -> dict[str, dict]:
        return archive_state.directory_objects(self._directory_lines(team, kind, with_object=True))

    def tracked_items(self) -> dict[ItemKey, TrackedItem]:
        return self._tracked_where("status != 'removed'")

    def removed_items(self) -> dict[ItemKey, TrackedItem]:
        return self._tracked_where("status = 'removed'")

    def downloads(self) -> dict[DownloadKey, DownloadState]:
        names = ", ".join(DOWNLOAD_COLUMNS)
        rows = self._con.execute(
            f"SELECT {names} FROM download_state ORDER BY team, target"
        ).fetchall()
        states = [download_state(dict(zip(DOWNLOAD_COLUMNS, row, strict=True))) for row in rows]
        return {(state.team_id, state.target): state for state in states}

    def append_run(self, run: RunRecord, slarc_version: str) -> None:
        """Commits the run row alone; raises ValueError if the open batch is not empty.

        The run row's tracked and downloads lists are empty: the tracked_item and
        download_state tables hold that state, and only commit writes them.
        """
        if self._batch or self._drops:
            raise ValueError("Commit or discard the open batch before appending the run")
        recorded = msgspec.structs.replace(run, tracked=[], downloads=[])
        with self._lock.write(), self._transaction():
            insert_rows(
                self._con, "run", RUN_COLUMNS, [run_row(run_envelope(recorded, slarc_version))]
            )

    def channels(self, team: str) -> list[str]:
        rows = self._con.execute(
            "SELECT DISTINCT channel FROM message_version WHERE team = ?", [team]
        ).fetchall()
        batched = {env.meta.channel for env in self._batch if self._is_message_of(env, team)}
        return sorted({row[0] for row in rows} | {c for c in batched if c is not None})

    def message_lines(self, team: str, channel: str) -> Iterator[Envelope]:
        names = ", ".join(f'"{name}"' for name in MESSAGE_COLUMNS)
        rows = self._con.execute(
            f"SELECT {names} FROM message_version WHERE team = ? AND channel = ? "
            f"ORDER BY ts, {VERSION_ORDER}",
            [team, channel],
        ).fetchall()
        lines = [message_envelope(dict(zip(MESSAGE_COLUMNS, row, strict=True))) for row in rows]
        return iter(lines + self._batch_messages(team, channel))

    def drop_messages(self, team: str, channel: str, stamps: set[str]) -> int:
        dropped = self._drop_batched(
            lambda m: (
                m.kind in MESSAGE_KINDS
                and (m.team, m.channel) == (team, channel)
                and m.ts in stamps
            )
        )
        return dropped + self._stage_drop(
            "message_version",
            "team = ? AND channel = ? AND list_contains(?, ts)",
            [team, channel, sorted(stamps)],
        )

    def drop_directory_lines(self, team: str, kind: Kind, ids: set[str]) -> int:
        dropped = self._drop_batched(lambda m: (m.team, m.kind) == (team, kind) and m.id in ids)
        return dropped + self._stage_drop(
            "directory_entry",
            "team = ? AND kind = ? AND list_contains(?, id)",
            [team, kind, sorted(ids)],
        )

    def drop_workspace(self, team: str) -> None:
        self._drop_batched(lambda m: m.team == team)
        self._stage_drop("message_version", "team = ?", [team])
        self._stage_drop("directory_entry", "team = ?", [team])

    def drop_downloads(self, team: str, targets: set[str]) -> None:
        self._stage_drop(
            "download_state", "team = ? AND list_contains(?, target)", [team, sorted(targets)]
        )

    def blob_references(self) -> list[tuple[str, Kind, str, str]]:
        rows = self._con.execute(
            "SELECT team, kind, id, blob FROM directory_entry "
            "WHERE blob IS NOT NULL AND id IS NOT NULL ORDER BY team, kind, id, rowid"
        ).fetchall()
        references: list[tuple[str, Kind, str, str]] = [tuple(row) for row in rows]
        for env in self._batch:
            meta = env.meta
            if meta.kind not in MESSAGE_KINDS and meta.id and meta.blob:
                references.append((meta.team or "", meta.kind, meta.id, meta.blob))
        return list(dict.fromkeys(references))

    def _directory_lines(self, team: str, kind: Kind, *, with_object: bool) -> list[Envelope]:
        rows = self._con.execute(
            f"SELECT id, fetched_at, run, hash, blob, {'object' if with_object else 'NULL'} "
            f"FROM directory_entry WHERE team = ? AND kind = ? ORDER BY {VERSION_ORDER}",
            [team, kind],
        ).fetchall()
        lines = [
            Envelope(
                meta=Meta(kind=kind, id=id_, fetched_at=fetched, run=run, hash=hash_, blob=blob),
                object=_decode.decode(obj) if obj is not None else None,
            )
            for id_, fetched, run, hash_, blob, obj in rows
        ]
        lines.extend(env for env in self._batch if (env.meta.team, env.meta.kind) == (team, kind))
        return lines

    def _tracked_where(self, condition: str) -> dict[ItemKey, TrackedItem]:
        names = ", ".join(f'"{name}"' for name in TRACKED_COLUMNS)
        rows = self._con.execute(
            f"SELECT {names} FROM tracked_item WHERE {condition} ORDER BY team, kind, channel, ts"
        ).fetchall()
        items = {}
        for row in rows:
            item = tracked_item(dict(zip(TRACKED_COLUMNS, row, strict=True)))
            items[item.key] = item
        return items

    def _commit(self, change: Commit) -> Committed:
        stored = self._stored_revisions()
        revisions = archive_state.item_revisions(change, stored)
        archive_state.require_complete_teams(change, stored)
        existing = self._download_keys() if change.downloads_exist else set()
        dropped = archive_state.dropped_downloads(change, existing)
        batch = archive_state.without_targets(self._batch, dropped)
        archive_state.require_live_teams(batch, stored, change.items)
        self._apply_drops()
        self._insert_batch(batch)
        if self._missing_directory_rows(change.requires):
            raise CommitConflict()
        self._upsert_tracked(
            [msgspec.structs.replace(item, revision=revisions[item.key]) for item in change.items]
        )
        self._upsert_downloads(
            [state for state in change.downloads if (state.team_id, state.target) not in dropped]
        )
        settled = [{"team": team, "target": target} for team, target in change.settled]
        self._delete_keys("download_state", DOWNLOAD_KEY, settled)
        return Committed(revisions=revisions, dropped=dropped)

    def _stored_revisions(self) -> archive_state.StoredRevisions:
        rows = self._con.execute(
            "SELECT team, kind, channel, ts, revision, status FROM tracked_item"
        ).fetchall()
        return {
            (team, kind, channel, ts): (rev, status)
            for team, kind, channel, ts, rev, status in rows
        }

    def _download_keys(self) -> set[DownloadKey]:
        rows = self._con.execute("SELECT team, target FROM download_state").fetchall()
        return {(team, target) for team, target in rows}

    def _missing_directory_rows(self, refs: Iterable[DirectoryRef]) -> bool:
        keys = [dict(zip(DIRECTORY_REF, ref, strict=True)) for ref in refs]
        if not keys:
            return False
        missing = self._con.execute(
            f"SELECT count(*) FROM (SELECT unnest(from_json(?::JSON, "
            f"'{_structure(dict.fromkeys(DIRECTORY_REF, 'VARCHAR'))}')) AS k) WHERE NOT EXISTS "
            "(SELECT 1 FROM directory_entry d "
            "WHERE d.team = k.team AND d.kind = k.kind AND d.id = k.id)",
            [msgspec.json.encode(keys).decode()],
        ).fetchall()
        return missing[0][0] > 0

    def _insert_batch(self, batch: list[Envelope]) -> None:
        messages = [env for env in batch if env.meta.kind in MESSAGE_KINDS]
        entries = [env for env in batch if env.meta.kind not in MESSAGE_KINDS]
        messages.sort(key=lambda env: (env.meta.team, env.meta.channel, env.meta.ts))
        entries.sort(key=lambda env: (env.meta.team, env.meta.kind, env.meta.id or ""))
        message_rows = [message_row(env) for env in messages]
        directory_rows = [directory_row(env) for env in entries]
        insert_rows(self._con, "message_version", MESSAGE_COLUMNS, message_rows)
        insert_rows(self._con, "directory_entry", DIRECTORY_COLUMNS, directory_rows)

    def _upsert_tracked(self, items: list[TrackedItem]) -> None:
        rows = [tracked_row(item) for item in items]
        self._delete_keys("tracked_item", TRACKED_KEY, rows)
        insert_rows(self._con, "tracked_item", TRACKED_COLUMNS, rows)

    def _upsert_downloads(self, states: list[DownloadState]) -> None:
        rows = [download_row(state) for state in states]
        self._delete_keys("download_state", DOWNLOAD_KEY, rows)
        insert_rows(self._con, "download_state", DOWNLOAD_COLUMNS, rows)

    def _delete_keys(self, table: str, key: tuple[str, ...], rows: list[dict]) -> None:
        if not rows:
            return
        keys = [{name: row[name] for name in key} for row in rows]
        matches = " AND ".join(f"k.{name} = {table}.{name}" for name in key)
        self._con.execute(
            f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM "
            f"(SELECT unnest(from_json(?::JSON, '{_structure(dict.fromkeys(key, 'VARCHAR'))}')) "
            f"AS k) WHERE {matches})",
            [msgspec.json.encode(keys).decode()],
        ).fetchall()

    def _stage_drop(self, table: str, condition: str, params: list) -> int:
        """Stages a DELETE for the next commit; returns the committed rows it matches now."""
        self._drops.append((table, condition, params))
        matched = self._con.execute(
            f"SELECT count(*) FROM {table} WHERE {condition}", params
        ).fetchall()
        return matched[0][0]

    def _apply_drops(self) -> None:
        for table, condition, params in self._drops:
            self._con.execute(f"DELETE FROM {table} WHERE {condition}", params).fetchall()

    @contextmanager
    def _transaction(self) -> Generator[None]:
        self._con.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")

    def _drop_batched(self, drop: Callable[[Meta], bool]) -> int:
        kept = [env for env in self._batch if not drop(env.meta)]
        dropped = len(self._batch) - len(kept)
        self._batch = kept
        return dropped

    def _batch_messages(self, team: str, channel: str) -> list[Envelope]:
        return [
            env
            for env in self._batch
            if self._is_message_of(env, team) and env.meta.channel == channel
        ]

    @staticmethod
    def _is_message_of(env: Envelope, team: str) -> bool:
        return env.meta.kind in MESSAGE_KINDS and env.meta.team == team


def insert_rows(
    con: duckdb.DuckDBPyConnection, table: str, columns: dict[str, str], rows: list[dict]
) -> None:
    """Inserts rows in order, passed as one JSON parameter."""
    if not rows:
        return
    names = ", ".join(f'"{name}"' for name in columns)
    con.execute(
        f"INSERT INTO {table} ({names}) SELECT unnest(r) FROM "
        f"(SELECT unnest(from_json(?::JSON, '{_structure(columns)}')) AS r)",
        [msgspec.json.encode(rows).decode()],
    )


def _structure(columns: dict[str, str]) -> str:
    return msgspec.json.encode([columns]).decode()


def message_row(env: Envelope) -> dict:
    meta = env.meta
    obj = env.object or {}
    return {
        "team": meta.team,
        "channel": meta.channel,
        "ts": meta.ts,
        "thread_ts": obj.get("thread_ts"),
        "user": obj.get("user"),
        "kind": meta.kind,
        "fetched_at": meta.fetched_at,
        "run": meta.run,
        "source_team": meta.source_team,
        "api": meta.api,
        "slarc": meta.slarc,
        "hash": meta.hash,
        "plain": meta.plain,
        "object": env.object,
    }


def message_envelope(row: dict) -> Envelope:
    meta = Meta(
        kind=row["kind"],
        team=row["team"],
        channel=row["channel"],
        ts=row["ts"],
        source_team=row["source_team"],
        fetched_at=row["fetched_at"],
        run=row["run"],
        api=row["api"],
        slarc=row["slarc"],
        hash=row["hash"],
        plain=row["plain"],
    )
    obj = row["object"]
    return Envelope(meta=meta, object=_decode.decode(obj) if obj is not None else None)


def directory_row(env: Envelope) -> dict:
    meta = env.meta
    return {
        "team": meta.team,
        "kind": meta.kind,
        "id": meta.id,
        "fetched_at": meta.fetched_at,
        "run": meta.run,
        "api": meta.api,
        "slarc": meta.slarc,
        "hash": meta.hash,
        "blob": meta.blob,
        "object": env.object,
    }


def directory_envelope(row: dict) -> Envelope:
    meta = Meta(
        kind=row["kind"],
        team=row["team"],
        id=row["id"],
        fetched_at=row["fetched_at"],
        run=row["run"],
        api=row["api"],
        slarc=row["slarc"],
        hash=row["hash"],
        blob=row["blob"],
    )
    obj = row["object"]
    return Envelope(meta=meta, object=_decode.decode(obj) if obj is not None else None)


def run_row(env: Envelope) -> dict:
    """The run row of a run line; its object is the run line's whole object."""
    obj = env.object or {}
    return {
        "run": env.meta.run,
        "command": obj.get("command"),
        "started": obj.get("started"),
        "finished": obj.get("finished"),
        "fetched_at": env.meta.fetched_at,
        "slarc": env.meta.slarc,
        "object": env.object,
    }


def run_line(row: dict) -> Envelope:
    meta = Meta(kind="run", fetched_at=row["fetched_at"], run=row["run"], slarc=row["slarc"])
    obj = row["object"]
    return Envelope(meta=meta, object=_decode.decode(obj) if obj is not None else None)


def tracked_row(item: TrackedItem) -> dict:
    row = msgspec.to_builtins(item)
    row["team"] = row.pop("team_id")
    row["ts"] = item.ts or ""
    return row


def tracked_item(row: dict) -> TrackedItem:
    row["team_id"] = row.pop("team")
    row["ts"] = row["ts"] or None
    row["last_read"] = _decode.decode(row["last_read"])
    row["retry_threads"] = _decode.decode(row["retry_threads"])
    return msgspec.convert(row, TrackedItem)


def download_row(state: DownloadState) -> dict:
    row = msgspec.to_builtins(state)
    row["team"] = row.pop("team_id")
    return row


def download_state(row: dict) -> DownloadState:
    row["team_id"] = row.pop("team")
    return msgspec.convert(row, DownloadState)
