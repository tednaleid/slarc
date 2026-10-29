# ABOUTME: Removal use case behind slarc rm: resolves selectors to tracked items from the archive.
# ABOUTME: Plans the removal and commits it with revision checks; maintenance frees the bytes.
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import msgspec

from slarc.domain.links import Selector
from slarc.domain.tracking import ItemKey, TrackedItem, WorkspaceSetting
from slarc.ports import (
    ArchiveStore,
    BlobStore,
    CommitConflict,
    DownloadKey,
    QueryIndex,
    SlarcError,
)
from slarc.service.deletion import Deletion, plan_deletion, stage
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
    now: datetime,
) -> Removal:
    """Resolves each (text as typed, selector) pair to tracked or removed items and plans what
    removing them deletes at now, messages already past their retention included.

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
    return _plan(list(named.values()), teams, tracked, now, archive, blobs)[0]


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
        current, deletion = _plan(named, removal.teams, tracked, uow.clock.now(), archive, blobs)
        kept = [
            item
            for key, item in tracked.items()
            if item.team_id in current.teams and key not in names
        ]
        stage(deletion, archive)
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
    now: datetime,
    archive: ArchiveStore,
    blobs: BlobStore,
) -> tuple[Removal, Deletion]:
    """The removal of the named items, and what it deletes, when tracked holds the items
    tracked now."""
    names = {item.key for item in named}
    items = [tracked[item.key] for item in named if item.key in tracked]
    remaining = {key: item for key, item in tracked.items() if key not in names}
    settings = archive.workspace_settings()
    deletion = plan_deletion(teams, remaining, settings, now, archive, blobs)
    removal = Removal(
        items=items,
        already_removed=[item for item in named if item.key not in tracked],
        teams=teams,
        wiped_teams=[team.team for team in deletion.teams if team.wiped],
        lines=deletion.lines,
        files=deletion.files,
        images=deletion.images,
        blobs=len(deletion.blobs),
        kept_messages=_kept_messages(items, remaining, settings, now, archive),
        downloads=[key for team in deletion.teams for key in team.downloads],
    )
    return removal, deletion


def _kept_messages(
    items: list[TrackedItem],
    remaining: dict[ItemKey, TrackedItem],
    settings: Mapping[str, WorkspaceSetting],
    now: datetime,
    archive: ArchiveStore,
) -> int:
    """Stored messages the items cover that the remaining tracked items also keep."""
    kept = 0
    places = dict.fromkeys((item.team_id, item.channel) for item in items)
    for team, channel in places:
        versions = message_versions(archive.message_lines(team, channel))
        named = covered_stamps(
            versions, [i for i in items if (i.team_id, i.channel) == (team, channel)], settings, now
        )
        others = [i for i in remaining.values() if (i.team_id, i.channel) == (team, channel)]
        kept += len(named & covered_stamps(versions, others, settings, now))
    return kept
