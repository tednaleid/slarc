# ABOUTME: List use cases: the archive's tracked items with message and download counts, and the
# ABOUTME: conversations a Slack session can see, marked when the archive tracks them.
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from slarc.config import Settings
from slarc.domain.directory import ConversationKind, DirectoryView, conversation_kind
from slarc.domain.envelope import Kind
from slarc.domain.links import Selector
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem, item_label
from slarc.ports import (
    ArchiveStore,
    DirectoryState,
    DownloadKey,
    QueryIndex,
    SlackConnector,
    SlarcError,
)
from slarc.service.auth import domain_of
from slarc.service.downloads import (
    QUEUED_STATUSES,
    SKIPPED_STATUSES,
    image_types,
    outstanding_downloads,
)
from slarc.service.selectors import items_named

WORKSPACE_DOMAINS = "SELECT team_id, domain FROM workspace"
KIND_ORDER: tuple[ConversationKind, ...] = ("channel", "group_dm", "dm")
# Saved first and failures last, so a scoped list ends with what needs attention.
DOWNLOAD_ORDER = (
    "saved",
    "pending",
    "deleted",
    "external",
    "too_large",
    "forbidden",
    "not_found",
    "gave_up",
    "failed",
)


@dataclass(frozen=True)
class ListRow:
    """A tracked item; pending and skipped count the downloads its messages reference."""

    workspace: str
    conversation: str
    kind: str
    messages: int
    last_pull: str | None
    pending: int
    skipped: int
    status: str


@dataclass(frozen=True)
class ListedDownload:
    """A download a listed item's messages reference. type is file, image, avatar or emoji.
    status is saved, or else its download state's status, with that state's attempts and last
    error."""

    workspace: str
    conversation: str
    type: str
    status: str
    attempts: int | None
    error: str | None
    url: str


@dataclass(frozen=True)
class ArchiveListing:
    """pending and skipped count every outstanding download, avatars and emoji included, or in a
    list scoped by selectors, those the listed items reference. downloads lists those, saved or
    not, only in a scoped list."""

    archive: Path
    config_files: list[Path]
    rows: list[ListRow]
    pending: int
    skipped: int
    downloads: list[ListedDownload] = field(default_factory=list)


@dataclass(frozen=True)
class RemoteRow:
    workspace: str
    channel: str
    conversation: str
    kind: ConversationKind
    tracked: bool


@dataclass(frozen=True)
class _Target:
    """A download a message references: its key, the kind of directory line that records it,
    its type for a listing, and its URL."""

    key: DownloadKey
    kind: Literal["file", "image"]
    type: str
    url: str


def list_archive(
    settings: Settings,
    archive: ArchiveStore,
    index: QueryIndex,
    selectors: Sequence[tuple[str, Selector]] = (),
) -> ArchiveListing:
    """Every tracked item, or with selectors, as (text as typed, selector) pairs, the items they
    name and every download their messages reference, sorted by DOWNLOAD_ORDER.

    Raises SlarcError when a selector names nothing tracked.
    """
    domains = workspace_domains(index)
    outstanding = outstanding_downloads(archive)
    pending = {key for key, state in outstanding.items() if state.status in QUEUED_STATUSES}
    skipped = {key for key, state in outstanding.items() if state.status in SKIPPED_STATUSES}
    views: dict[str, DirectoryView] = {}
    stored: dict[tuple[str, Kind], dict[str, DirectoryState]] = {}
    rows = []
    downloads: list[ListedDownload] = []
    referenced: set[DownloadKey] = set()
    for item in _listed(archive, domains, selectors):
        if item.team_id not in views:
            views[item.team_id] = _directory(archive, item.team_id)
        messages = _messages(index, item)
        targets: dict[DownloadKey, _Target] = {}
        for message in messages:
            for target in _download_targets(
                item.team_id, message["object"] or {}, views[item.team_id]
            ):
                targets.setdefault(target.key, target)
        referenced |= targets.keys()
        row = ListRow(
            workspace=domains.get(item.team_id) or item.team_id,
            conversation=item_label(item, views[item.team_id]),
            kind=item.kind,
            messages=len(messages),
            last_pull=item.last_pull_at,
            pending=len(targets.keys() & pending),
            skipped=len(targets.keys() & skipped),
            status=item.status,
        )
        rows.append(row)
        if selectors:
            downloads += _downloads(row, targets.values(), outstanding, archive, stored)
    rows.sort(key=lambda row: (row.workspace, row.conversation, row.kind))
    downloads.sort(
        key=lambda d: (
            DOWNLOAD_ORDER.index(d.status),
            d.workspace,
            d.conversation,
            d.attempts or 0,
            d.error or "",
            d.url,
        )
    )
    if selectors:
        pending, skipped = pending & referenced, skipped & referenced
    return ArchiveListing(
        settings.archive, list(settings.sources), rows, len(pending), len(skipped), downloads
    )


def workspace_domains(index: QueryIndex) -> dict[str, str]:
    """The Slack domain of each workspace a run has authenticated, by team id."""
    return {team_id: domain for team_id, domain in index.sql(WORKSPACE_DOMAINS)[1] if domain}


def list_remote(
    connector: SlackConnector, workspace: str, archive: ArchiveStore
) -> list[RemoteRow]:
    """Every conversation the session for workspace can see; tracked marks tracked conversations."""
    gateway = connector.connect(workspace)
    info = gateway.auth_test()
    team_id = info["team_id"]
    tracked = {
        item.channel
        for item in archive.tracked_items().values()
        if item.team_id == team_id and item.kind == "conversation"
    }
    conversations = {
        conversation["id"]: conversation for conversation in gateway.list_conversations()
    }
    view = DirectoryView(
        users=archive.directory_objects(team_id, "user"),
        conversations=conversations,
        usergroups={},
        emoji={},
        bots={},
    )
    rows = [
        RemoteRow(
            workspace=domain_of(info) or team_id,
            channel=channel,
            conversation=view.conversation_label(channel),
            kind=conversation_kind(conversation),
            tracked=channel in tracked,
        )
        for channel, conversation in conversations.items()
    ]
    return sorted(rows, key=lambda row: (KIND_ORDER.index(row.kind), row.conversation))


def _listed(
    archive: ArchiveStore,
    domains: dict[str, str],
    selectors: Sequence[tuple[str, Selector]],
) -> list[TrackedItem]:
    tracked = archive.tracked_items()
    if not selectors:
        return list(tracked.values())
    teams = {domain: team for team, domain in domains.items()}
    named: dict[ItemKey, TrackedItem] = {}
    for text, selector in selectors:
        found = items_named(selector, False, tracked, {}, teams, archive)
        if not found:
            raise SlarcError(f"Nothing tracked matches {text}.")
        for item in found:
            named.setdefault(item.key, item)
    return list(named.values())


def _downloads(
    row: ListRow,
    targets: Iterable[_Target],
    outstanding: dict[DownloadKey, DownloadState],
    archive: ArchiveStore,
    stored: dict[tuple[str, Kind], dict[str, DirectoryState]],
) -> list[ListedDownload]:
    """Each target that is saved or has a download state; stored caches directory states."""
    found = []
    for target in targets:
        state = outstanding.get(target.key)
        if state is not None:
            status, attempts, error = state.status, state.attempts, state.last_error
            url = state.url or target.url
        elif _saved(target, archive, stored):
            status, attempts, error, url = "saved", None, None, target.url
        else:
            continue
        found.append(
            ListedDownload(
                row.workspace, row.conversation, target.type, status, attempts, error, url
            )
        )
    return found


def _saved(
    target: _Target,
    archive: ArchiveStore,
    stored: dict[tuple[str, Kind], dict[str, DirectoryState]],
) -> bool:
    kind_key = (target.key[0], target.kind)
    if kind_key not in stored:
        stored[kind_key] = archive.directory_state(*kind_key)
    held = stored[kind_key].get(target.key[1])
    return held is not None and held.blob is not None


def _directory(archive: ArchiveStore, team_id: str) -> DirectoryView:
    emoji = archive.directory_objects(team_id, "emoji")
    return DirectoryView(
        users=archive.directory_objects(team_id, "user"),
        conversations=archive.directory_objects(team_id, "conversation"),
        usergroups={},
        emoji={name: obj.get("url", "") for name, obj in emoji.items()},
        bots={},
    )


def _messages(index: QueryIndex, item: TrackedItem) -> list[dict]:
    """The current messages a tracked item covers: its range of a conversation, or its thread."""
    if item.kind == "conversation":
        return index.messages(item.team_id, item.channel, item.since, item.until, False)
    return index.thread(item.team_id, item.channel, item.ts or "", False)


def _download_targets(team_id: str, message: dict, view: DirectoryView) -> list[_Target]:
    """The files and images a message references, as pull queues their downloads."""
    files = [
        _Target(
            (team_id, file["id"]),
            "file",
            "file",
            file.get("url_private_download") or file.get("url_private") or file["id"],
        )
        for file in message.get("files") or []
        if file.get("id")
    ]
    images = [
        _Target((team_id, url), "image", kind, url)
        for url, kind in image_types(message, view).items()
    ]
    return files + images
