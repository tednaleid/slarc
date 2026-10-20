# ABOUTME: Removal use case behind slarc rm: resolves selectors to tracked items from the archive.
# ABOUTME: Plans and records the removal, then sweeps message lines, files, images and blobs.
from collections.abc import Iterable
from dataclasses import dataclass

import msgspec

from slarc.domain.directory import directory_image_url
from slarc.domain.envelope import Envelope
from slarc.domain.links import ConversationSel, Selector, WorkspaceSel
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem
from slarc.ports import ArchiveStore, BlobStore, Commit, QueryIndex, SlarcError
from slarc.service.downloads import image_urls
from slarc.service.listing import workspace_domains
from slarc.service.selectors import archived_conversation, only_workspace
from slarc.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class Removal:
    """What rm stops tracking and what its sweep deletes.

    lines counts message and deleted lines, every version; files and images count targets.
    kept_messages counts stored messages the named items cover that other tracked items still
    cover. downloads are the final states that keep a later pull from fetching orphaned targets.
    """

    items: list[TrackedItem]
    already_removed: list[TrackedItem]
    teams: list[str]
    wiped_teams: list[str]
    lines: int
    files: int
    images: int
    blobs: int
    kept_messages: int
    downloads: list[DownloadState]

    @property
    def nothing_to_do(self) -> bool:
        deletes = self.lines or self.files or self.images or self.blobs or self.downloads
        return not self.items and not deletes


def plan_removal(
    selectors: list[tuple[str, Selector]],
    channel_mode: bool,
    archive: ArchiveStore,
    blobs: BlobStore,
    index: QueryIndex,
) -> Removal:
    """Resolves each (text as typed, selector) pair to tracked or removed items and plans the sweep.

    A selector naming only removed items is accepted, so running rm again finishes its sweep.
    """
    tracked = archive.tracked_items()
    removed = archive.removed_items()
    domains = {domain: team for team, domain in workspace_domains(index).items()}
    named: dict[ItemKey, TrackedItem] = {}
    for text, selector in selectors:
        found = _named(selector, channel_mode, tracked, removed, domains, archive)
        if not found:
            raise SlarcError(f"Nothing tracked matches {text}.")
        for item in found:
            named.setdefault(item.key, item)
    items = [item for key, item in named.items() if key in tracked]
    remaining = {key: item for key, item in tracked.items() if key not in named}
    teams = list(dict.fromkeys(item.team_id for item in named.values()))
    deletion = _deletion(teams, remaining, archive, blobs)
    return Removal(
        items=items,
        already_removed=[item for key, item in named.items() if key not in tracked],
        teams=teams,
        wiped_teams=[team.team for team in deletion.teams if team.wiped],
        lines=sum(team.lines for team in deletion.teams),
        files=sum(len(team.files) for team in deletion.teams),
        images=sum(len(team.images) for team in deletion.teams),
        blobs=len(deletion.blobs),
        kept_messages=_kept_messages(items, remaining, archive),
        downloads=[state for team in deletion.teams for state in team.downloads],
    )


def apply_removal(removal: Removal, uow: UnitOfWork) -> None:
    """Records the removal in the run; the unit of work appends the run line on exit."""
    run = uow.run
    run.teams = list(removal.teams)
    run.tracked.extend(msgspec.structs.replace(item, status="removed") for item in removal.items)
    run.downloads.extend(removal.downloads)
    run.counts["lines_deleted"] = removal.lines
    run.counts["files_deleted"] = removal.files
    run.counts["images_deleted"] = removal.images
    run.counts["blobs_deleted"] = removal.blobs


def sweep(teams: list[str], archive: ArchiveStore, blobs: BlobStore) -> None:
    """Deletes what no tracked item needs in the teams, and blobs no directory line references.

    It reads the tracked items afresh, so running it again after an interrupt finishes it.
    """
    deletion = _deletion(teams, archive.tracked_items(), archive, blobs)
    for team in deletion.teams:
        if team.wiped:
            archive.drop_workspace(team.team)
            continue
        for channel, stamps in team.stamps.items():
            archive.drop_messages(team.team, channel, stamps)
        if team.files:
            archive.drop_directory_lines(team.team, "file", team.files)
        if team.images:
            archive.drop_directory_lines(team.team, "image", team.images)
    archive.commit(Commit())
    for blob in sorted(deletion.blobs):
        blobs.delete(blob)


@dataclass(frozen=True)
class _TeamDeletion:
    """One workspace's share of a sweep; a wiped workspace has nothing left tracked."""

    team: str
    wiped: bool
    stamps: dict[str, set[str]]
    lines: int
    files: set[str]
    images: set[str]
    downloads: list[DownloadState]


@dataclass(frozen=True)
class _Deletion:
    teams: list[_TeamDeletion]
    blobs: set[str]


def _deletion(
    teams: list[str],
    tracked: dict[ItemKey, TrackedItem],
    archive: ArchiveStore,
    blobs: BlobStore,
) -> _Deletion:
    """What a sweep of the teams deletes when tracked holds the items still tracked."""
    known = archive.downloads()
    deletions = [
        _team_deletion(team, [i for i in tracked.values() if i.team_id == team], known, archive)
        for team in teams
    ]
    dropped = set()
    wiped = set()
    for team in deletions:
        if team.wiped:
            wiped.add(team.team)
        dropped |= {(team.team, "file", id_) for id_ in team.files}
        dropped |= {(team.team, "image", id_) for id_ in team.images}
    referenced = {
        blob
        for team, kind, id_, blob in archive.blob_references()
        if team not in wiped and (team, kind, id_) not in dropped
    }
    return _Deletion(deletions, set(blobs.ids()) - referenced)


def _team_deletion(
    team: str,
    tracked: list[TrackedItem],
    known: dict[tuple[str, str], DownloadState],
    archive: ArchiveStore,
) -> _TeamDeletion:
    """Uncovered message lines, then the files and images no kept message line references.

    Avatars and custom emoji images stay while anything in the workspace is tracked.
    """
    stamps: dict[str, set[str]] = {}
    lines = 0
    file_refs: set[str] = set()
    image_refs: set[str] = set()
    for channel in archive.channels(team):
        versions = _versions(archive.message_lines(team, channel))
        covered = _covered(versions, [item for item in tracked if item.channel == channel])
        uncovered = set(versions) - covered
        if uncovered:
            stamps[channel] = uncovered
            lines += sum(len(versions[ts]) for ts in uncovered)
        for ts in covered:
            for env in versions[ts]:
                message = env.object or {}
                file_refs.update(file["id"] for file in message.get("files") or [])
                image_refs.update(image_urls(message))
    if tracked:
        image_refs |= _directory_images(team, archive)
    files = set(archive.directory_state(team, "file")) - file_refs
    images = set(archive.directory_state(team, "image")) - image_refs
    downloads = [
        msgspec.structs.replace(state, status="removed")
        for state in known.values()
        if state.team_id == team
        and state.status != "removed"
        and state.target not in (file_refs if state.target_kind == "file" else image_refs)
    ]
    return _TeamDeletion(team, not tracked, stamps, lines, files, images, downloads)


def _directory_images(team: str, archive: ArchiveStore) -> set[str]:
    """The current avatar and custom emoji image URLs of the workspace's directory."""
    urls = set()
    for kind in ("user", "emoji"):
        for obj in archive.directory_objects(team, kind).values():
            url = directory_image_url(kind, obj)
            if url is not None:
                urls.add(url)
    return urls


def _versions(lines: Iterable[Envelope]) -> dict[str, list[Envelope]]:
    """Each stored message's lines by ts: every version and deleted marker."""
    versions: dict[str, list[Envelope]] = {}
    for env in lines:
        if env.meta.ts is not None:
            versions.setdefault(env.meta.ts, []).append(env)
    return versions


def _covered(versions: dict[str, list[Envelope]], items: list[TrackedItem]) -> set[str]:
    """The stored messages of one channel that its tracked items cover.

    A conversation covers all of them. A thread or message covers the message at its ts and
    every message whose thread_ts in any version is that ts, since pull stores its replies.
    """
    if any(item.kind == "conversation" for item in items):
        return set(versions)
    parents = {item.ts for item in items}
    return {
        ts
        for ts, lines in versions.items()
        if ts in parents or any((env.object or {}).get("thread_ts") in parents for env in lines)
    }


def _kept_messages(
    items: list[TrackedItem], remaining: dict[ItemKey, TrackedItem], archive: ArchiveStore
) -> int:
    """Stored messages the items cover that the remaining tracked items also cover."""
    kept = 0
    places = dict.fromkeys((item.team_id, item.channel) for item in items)
    for team, channel in places:
        versions = _versions(archive.message_lines(team, channel))
        named = _covered(versions, [i for i in items if (i.team_id, i.channel) == (team, channel)])
        others = [i for i in remaining.values() if (i.team_id, i.channel) == (team, channel)]
        kept += len(named & _covered(versions, others))
    return kept


def _named(
    selector: Selector,
    channel_mode: bool,
    tracked: dict[ItemKey, TrackedItem],
    removed: dict[ItemKey, TrackedItem],
    domains: dict[str, str],
    archive: ArchiveStore,
) -> list[TrackedItem]:
    """The tracked or removed items a selector names, resolved without Slack.

    A selector without a link names the only workspace with tracked items, or with removed
    items when nothing is tracked.
    """
    candidates = {**removed, **tracked}
    if not candidates:
        return []
    if selector.domain is None:
        team = only_workspace(tracked or removed)
    else:
        team = domains.get(selector.domain)
    if team is None:
        return []
    in_team = [item for item in candidates.values() if item.team_id == team]
    if isinstance(selector, WorkspaceSel):
        return in_team
    if isinstance(selector, ConversationSel):
        try:
            channel = archived_conversation(selector, team, archive)
        except SlarcError:
            return []
        wanted = {("conversation", channel, "")}
    elif channel_mode:
        wanted = {("conversation", selector.channel, "")}
    elif selector.thread_ts is not None:
        wanted = {("thread", selector.channel, str(selector.thread_ts))}
    else:
        ts = str(selector.ts)
        wanted = {(kind, selector.channel, ts) for kind in ("thread", "message")}
        if not any(item.key[1:] in wanted for item in in_team):
            wanted = {("thread", selector.channel, _thread_of(team, selector.channel, ts, archive))}
    return [item for item in in_team if item.key[1:] in wanted]


def _thread_of(team: str, channel: str, ts: str, archive: ArchiveStore) -> str:
    """The thread a stored message belongs to, as pull resolves a link to a reply."""
    stored = archive.stored_messages(team, channel).get(ts)
    return (stored.thread_ts if stored is not None else None) or ts
