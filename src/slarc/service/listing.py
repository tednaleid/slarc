# ABOUTME: List use cases: the archive's tracked items with message and download counts, and the
# ABOUTME: conversations a Slack session can see, marked when the archive tracks them.
from dataclasses import dataclass
from pathlib import Path

from slarc.config import Settings
from slarc.domain.directory import ConversationKind, DirectoryView, conversation_kind
from slarc.domain.tracking import TrackedItem, item_label
from slarc.ports import ArchiveStore, QueryIndex, SlackConnector
from slarc.service.auth import domain_of
from slarc.service.downloads import (
    QUEUED_STATUSES,
    SKIPPED_STATUSES,
    image_urls,
    outstanding_downloads,
)

WORKSPACE_DOMAINS = "SELECT team_id, domain FROM workspace"
KIND_ORDER: tuple[ConversationKind, ...] = ("channel", "group_dm", "dm")


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
class ArchiveListing:
    """pending and skipped count every outstanding download, avatars and emoji included."""

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


def list_archive(settings: Settings, archive: ArchiveStore, index: QueryIndex) -> ArchiveListing:
    domains = workspace_domains(index)
    outstanding = outstanding_downloads(archive)
    pending = {key for key, state in outstanding.items() if state.status in QUEUED_STATUSES}
    skipped = {key for key, state in outstanding.items() if state.status in SKIPPED_STATUSES}
    views: dict[str, DirectoryView] = {}
    rows = []
    for item in archive.tracked_items().values():
        if item.team_id not in views:
            views[item.team_id] = _directory(archive, item.team_id)
        messages = _messages(index, item)
        targets = {
            (item.team_id, target)
            for message in messages
            for target in _download_targets(message["object"] or {})
        }
        rows.append(
            ListRow(
                workspace=domains.get(item.team_id) or item.team_id,
                conversation=item_label(item, views[item.team_id]),
                kind=item.kind,
                messages=len(messages),
                last_pull=item.last_pull_at,
                pending=len(targets & pending),
                skipped=len(targets & skipped),
                status=item.status,
            )
        )
    rows.sort(key=lambda row: (row.workspace, row.conversation, row.kind))
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


def _directory(archive: ArchiveStore, team_id: str) -> DirectoryView:
    return DirectoryView(
        users=archive.directory_objects(team_id, "user"),
        conversations=archive.directory_objects(team_id, "conversation"),
        usergroups={},
        emoji={},
        bots={},
    )


def _messages(index: QueryIndex, item: TrackedItem) -> list[dict]:
    """The current messages a tracked item covers: its range of a conversation, or its thread."""
    if item.kind == "conversation":
        return index.messages(item.team_id, item.channel, item.since, item.until, False)
    return index.thread(item.team_id, item.channel, item.ts or "", False)


def _download_targets(message: dict) -> list[str]:
    """The file ids and image URLs a message references, which key its downloads."""
    files = [file["id"] for file in message.get("files") or [] if file.get("id")]
    return files + image_urls(message)
