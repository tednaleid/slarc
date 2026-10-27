# ABOUTME: Tracked items, download states and the run record written as a run line, and which
# ABOUTME: messages an item covers; converts a RunRecord to and from its run envelope.
from typing import Literal

import msgspec

from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Envelope, Meta
from slarc.domain.links import MessageTs

type ItemKey = tuple[str, str, str, str]


class TrackedItem(msgspec.Struct, kw_only=True):
    team_id: str
    kind: Literal["conversation", "thread", "message"]
    channel: str
    ts: str | None = None
    revision: int = 0
    since: str | None = None
    until: str | None = None
    newest_ts: str | None = None
    oldest_ts: str | None = None
    backfill_complete: bool = False
    last_search_at: str | None = None
    last_pull_at: str | None = None
    status: Literal["ok", "not_visible", "removed"] = "ok"
    archived: bool = False
    last_read: dict[str, str] = {}
    retry_threads: list[str] = []

    @property
    def key(self) -> ItemKey:
        return (self.team_id, self.kind, self.channel, self.ts or "")


def item_label(item: TrackedItem, view: DirectoryView) -> str:
    """The conversation's label, followed by the kind and ts for a thread or message."""
    where = view.conversation_label(item.channel)
    return where if item.kind == "conversation" else f"{where} {item.kind} {item.ts}"


def covers(item: TrackedItem, channel: str, ts: str, thread_ts: str | None) -> bool:
    """Whether the item tracks the message: a conversation item each thread whose parent falls
    in its window, a thread item its parent and replies, a message item that message."""
    if item.channel != channel:
        return False
    if item.kind == "conversation":
        parent = MessageTs(thread_ts or ts)
        return (item.since is None or parent > item.since) and (
            item.until is None or parent < item.until
        )
    if item.kind == "thread":
        return item.ts in (ts, thread_ts)
    return item.ts == ts


class DownloadState(msgspec.Struct, kw_only=True, frozen=True):
    team_id: str
    target_kind: Literal["file", "image"]
    target: str
    url: str | None = None
    status: Literal[
        "pending",
        "failed",
        "forbidden",
        "not_found",
        "too_large",
        "deleted",
        "external",
        "gave_up",
    ]
    attempts: int = 0
    last_error: str | None = None


class RunRecord(msgspec.Struct, kw_only=True):
    run: str
    command: str
    args: list[str] = []
    started: str
    finished: str | None = None
    teams: list[str] = []
    auth: list[dict] = []
    counts: dict[str, float | dict[str, int]] = {}
    errors: list[dict] = []
    tracked: list[TrackedItem] = []
    downloads: list[DownloadState] = []


def run_envelope(run: RunRecord, slarc_version: str) -> Envelope:
    meta = Meta(
        kind="run",
        fetched_at=run.finished or run.started,
        run=run.run,
        slarc=slarc_version,
    )
    return Envelope(meta=meta, object=msgspec.to_builtins(run))


def run_from_envelope(env: Envelope) -> RunRecord:
    return msgspec.convert(env.object, RunRecord)
