# ABOUTME: List use cases: the archive's tracked items with message and download counts, and the
# ABOUTME: conversations a Slack session can see, marked when the archive tracks them.
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from slarc.config import Settings
from slarc.domain.directory import ConversationKind, DirectoryView, conversation_kind
from slarc.domain.links import Selector
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem, item_label
from slarc.ports import ArchiveStore, DownloadKey, QueryIndex, SlackConnector, SlarcError
from slarc.service.auth import domain_of
from slarc.service.downloads import (
    QUEUED_STATUSES,
    SKIPPED_STATUSES,
    named_images,
    outstanding_downloads,
)
from slarc.service.selectors import items_named

WORKSPACE_DOMAINS = "SELECT team_id, domain FROM workspace"
KIND_ORDER: tuple[ConversationKind, ...] = ("channel", "group_dm", "dm")
# Failures first, then downloads skipped for good, then those not tried yet.
OUTSTANDING_ORDER = ("failed", "gave_up", "forbidden", "too_large", "pending")


@dataclass(frozen=True)
class Outstanding:
    """A download a tracked item's messages reference that is not saved; name is the file's
    name, :name: for a custom emoji or @name for an avatar, when there is one."""

    kind: str
    name: str | None
    url: str
    status: str
    attempts: int
    error: str | None


@dataclass(frozen=True)
class ListRow:
    """A tracked item; pending and skipped count the downloads its messages reference, and
    outstanding lists them in a list scoped by selectors."""

    workspace: str
    conversation: str
    kind: str
    messages: int
    last_pull: str | None
    pending: int
    skipped: int
    status: str
    outstanding: tuple[Outstanding, ...] = ()


@dataclass(frozen=True)
class ArchiveListing:
    """pending and skipped count every outstanding download, avatars and emoji included, or in a
    list scoped by selectors, those the listed items reference."""

    archive: Path
    config_files: list[Path]
    rows: list[ListRow]
    pending: int
    skipped: int


@dataclass(frozen=True)
class RemoteRow:
    workspace: str
    channel: str
    conversation: str
    kind: ConversationKind
    tracked: bool


def list_archive(
    settings: Settings,
    archive: ArchiveStore,
    index: QueryIndex,
    selectors: Sequence[tuple[str, Selector]] = (),
) -> ArchiveListing:
    """Every tracked item, or with selectors, as (text as typed, selector) pairs, the items they
    name with the downloads each one has not saved.

    Raises SlarcError when a selector names nothing tracked.
    """
    domains = workspace_domains(index)
    outstanding = outstanding_downloads(archive)
    pending = {key for key, state in outstanding.items() if state.status in QUEUED_STATUSES}
    skipped = {key for key, state in outstanding.items() if state.status in SKIPPED_STATUSES}
    views: dict[str, DirectoryView] = {}
    rows = []
    referenced: set[DownloadKey] = set()
    for item in _listed(archive, domains, selectors):
        if item.team_id not in views:
            views[item.team_id] = _directory(archive, item.team_id)
        messages = _messages(index, item)
        targets: dict[DownloadKey, str | None] = {}
        for message in messages:
            for target, name in _download_targets(message["object"] or {}, views[item.team_id]):
                targets.setdefault((item.team_id, target), name)
        referenced |= targets.keys()
        rows.append(
            ListRow(
                workspace=domains.get(item.team_id) or item.team_id,
                conversation=item_label(item, views[item.team_id]),
                kind=item.kind,
                messages=len(messages),
                last_pull=item.last_pull_at,
                pending=len(targets.keys() & pending),
                skipped=len(targets.keys() & skipped),
                status=item.status,
                outstanding=_outstanding(targets, outstanding, pending | skipped)
                if selectors
                else (),
            )
        )
    rows.sort(key=lambda row: (row.workspace, row.conversation, row.kind))
    if selectors:
        pending, skipped = pending & referenced, skipped & referenced
    return ArchiveListing(
        settings.archive, list(settings.sources), rows, len(pending), len(skipped)
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


def _outstanding(
    targets: dict[DownloadKey, str | None],
    states: dict[DownloadKey, DownloadState],
    listed: set[DownloadKey],
) -> tuple[Outstanding, ...]:
    found = [
        Outstanding(
            kind=states[key].target_kind,
            name=name,
            url=states[key].url or states[key].target,
            status=states[key].status,
            attempts=states[key].attempts,
            error=states[key].last_error,
        )
        for key, name in targets.items()
        if key in listed
    ]
    found.sort(key=lambda o: (OUTSTANDING_ORDER.index(o.status), o.attempts, o.error or "", o.url))
    return tuple(found)


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


def _download_targets(message: dict, view: DirectoryView) -> list[tuple[str, str | None]]:
    """The file ids and image URLs a message references, which key its downloads, each with
    its name: the file's name, or as named_images names an image."""
    files = [
        (file["id"], file.get("name")) for file in message.get("files") or [] if file.get("id")
    ]
    return files + list(named_images(message, view).items())
