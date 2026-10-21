# ABOUTME: Removal use case behind slarc rm: resolves selectors to tracked items from the archive.
# ABOUTME: Plans the removal and commits it with revision checks; maintenance frees the bytes.
from dataclasses import dataclass

import msgspec

from slarc.domain.directory import directory_image_url
from slarc.domain.links import Selector
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem
from slarc.ports import (
    ArchiveStore,
    BlobStore,
    CommitConflict,
    DownloadKey,
    QueryIndex,
    SlarcError,
)
from slarc.service.downloads import image_urls
from slarc.service.listing import workspace_domains
from slarc.service.selectors import covered_stamps, items_named, message_versions
from slarc.unit_of_work import UnitOfWork

REMOVAL_ATTEMPTS = 3
KEPT_CHANGING = "Other slarc commands kept changing these items; run rm again."


@dataclass(frozen=True)
class Removal:
    """What rm stops tracking and what its commit deletes.

    lines counts message and deleted lines, every version; files and images count targets;
    blobs counts the blobs no directory line references once the removal commits, which the
    run's maintenance deletes if the archive is idle.
    kept_messages counts stored messages the named items cover that other tracked items still
    cover. downloads are the targets whose download states the removal deletes, so a later pull
    does not fetch them and a running pull drops their results.
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
    downloads: list[DownloadKey]

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
    """Resolves each (text as typed, selector) pair to tracked or removed items and plans what
    removing them deletes.

    A selector naming only removed items is accepted, so running rm again deletes what is left.
    """
    tracked = archive.tracked_items()
    removed = archive.removed_items()
    domains = {domain: team for team, domain in workspace_domains(index).items()}
    named: dict[ItemKey, TrackedItem] = {}
    for text, selector in selectors:
        found = items_named(selector, channel_mode, tracked, removed, domains, archive)
        if not found:
            raise SlarcError(f"Nothing tracked matches {text}.")
        for item in found:
            named.setdefault(item.key, item)
    teams = list(dict.fromkeys(item.team_id for item in named.values()))
    return _plan(list(named.values()), teams, tracked, archive, blobs)[0]


def apply_removal(removal: Removal, uow: UnitOfWork, blobs: BlobStore) -> Removal:
    """Commits the removal of the items the plan names and returns the plan it committed.

    Each attempt recomputes the plan from fresh reads, stages its drops, and commits the named
    items as removed with a revision check on every tracked item in the plan's workspaces. A
    command that committed to those items, or started tracking another item in those
    workspaces, in between makes the commit conflict, and the next attempt recomputes; after
    REMOVAL_ATTEMPTS conflicts it raises SlarcError.
    """
    archive = uow.archive
    named = [*removal.items, *removal.already_removed]
    names = {item.key for item in named}
    uow.run.teams = list(removal.teams)
    for _ in range(REMOVAL_ATTEMPTS):
        tracked = archive.tracked_items()
        current, deletion = _plan(named, removal.teams, tracked, archive, blobs)
        kept = [
            item
            for key, item in tracked.items()
            if item.team_id in current.teams and key not in names
        ]
        _stage(deletion, archive)
        try:
            uow.commit(
                [msgspec.structs.replace(item, status="removed") for item in current.items],
                unchanged=kept,
                complete_teams=current.teams,
            )
        except CommitConflict:
            continue
        uow.run.counts["lines_deleted"] = current.lines
        uow.run.counts["files_deleted"] = current.files
        uow.run.counts["images_deleted"] = current.images
        uow.run.counts["blobs_deleted"] = current.blobs
        return current
    raise SlarcError(KEPT_CHANGING)


def _plan(
    named: list[TrackedItem],
    teams: list[str],
    tracked: dict[ItemKey, TrackedItem],
    archive: ArchiveStore,
    blobs: BlobStore,
) -> tuple[Removal, _Deletion]:
    """The removal of the named items, and what it deletes, when tracked holds the items
    tracked now."""
    names = {item.key for item in named}
    items = [tracked[item.key] for item in named if item.key in tracked]
    remaining = {key: item for key, item in tracked.items() if key not in names}
    deletion = _deletion(teams, remaining, archive, blobs)
    removal = Removal(
        items=items,
        already_removed=[item for item in named if item.key not in tracked],
        teams=teams,
        wiped_teams=[team.team for team in deletion.teams if team.wiped],
        lines=sum(team.lines for team in deletion.teams),
        files=sum(len(team.files) for team in deletion.teams),
        images=sum(len(team.images) for team in deletion.teams),
        blobs=len(deletion.blobs),
        kept_messages=_kept_messages(items, remaining, archive),
        downloads=[key for team in deletion.teams for key in team.downloads],
    )
    return removal, deletion


def _stage(deletion: _Deletion, archive: ArchiveStore) -> None:
    """Stages the drops of the deletion's message lines, files, images and download states."""
    for team in deletion.teams:
        if team.wiped:
            archive.drop_workspace(team.team)
        else:
            for channel, stamps in team.stamps.items():
                archive.drop_messages(team.team, channel, stamps)
            if team.files:
                archive.drop_directory_lines(team.team, "file", team.files)
            if team.images:
                archive.drop_directory_lines(team.team, "image", team.images)
        if team.downloads:
            archive.drop_downloads(team.team, {target for _, target in team.downloads})


@dataclass(frozen=True)
class _TeamDeletion:
    """One workspace's share of a removal; a wiped workspace has nothing left tracked."""

    team: str
    wiped: bool
    stamps: dict[str, set[str]]
    lines: int
    files: set[str]
    images: set[str]
    downloads: list[DownloadKey]


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
    """What a removal in the teams deletes when tracked holds the items still tracked."""
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
    known: dict[DownloadKey, DownloadState],
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
        versions = message_versions(archive.message_lines(team, channel))
        covered = covered_stamps(versions, [item for item in tracked if item.channel == channel])
        uncovered = set(versions) - covered
        if uncovered:
            stamps[channel] = uncovered
            lines += sum(len(versions[ts]) for ts in uncovered)
        for ts in covered:
            for env in versions[ts]:
                message = env.object or {}
                file_refs.update(f["id"] for f in message.get("files") or [] if f.get("id"))
                image_refs.update(image_urls(message))
    if tracked:
        image_refs |= _directory_images(team, archive)
    files = set(archive.directory_state(team, "file")) - file_refs
    images = set(archive.directory_state(team, "image")) - image_refs
    downloads = [
        key
        for key, state in known.items()
        if state.team_id == team
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


def _kept_messages(
    items: list[TrackedItem], remaining: dict[ItemKey, TrackedItem], archive: ArchiveStore
) -> int:
    """Stored messages the items cover that the remaining tracked items also cover."""
    kept = 0
    places = dict.fromkeys((item.team_id, item.channel) for item in items)
    for team, channel in places:
        versions = message_versions(archive.message_lines(team, channel))
        named = covered_stamps(
            versions, [i for i in items if (i.team_id, i.channel) == (team, channel)]
        )
        others = [i for i in remaining.values() if (i.team_id, i.channel) == (team, channel)]
        kept += len(named & covered_stamps(versions, others))
    return kept
