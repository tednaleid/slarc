# ABOUTME: Queues downloads of the files a message references, as pending entries in the run.
# ABOUTME: Skips files already downloaded, already queued or in a terminal download state.
from slarc.domain.tracking import DownloadState
from slarc.ports import DirectoryState, DownloadJob
from slarc.unit_of_work import UnitOfWork

UNDOWNLOADABLE_MODES = ("tombstone", "hidden_by_limit", "external")
TERMINAL_STATUSES = ("forbidden", "deleted", "external")


def queue_downloads(
    message: dict,
    team_id: str,
    uow: UnitOfWork,
    max_bytes: int,
    *,
    files: dict[str, DirectoryState] | None = None,
    known: dict[tuple[str, str], DownloadState] | None = None,
) -> list[DownloadJob]:
    """files and known, the archive's file and download state, are read here when not given."""
    referenced = message.get("files") or []
    if not referenced:
        return []
    stored = uow.archive.directory_state(team_id, "file") if files is None else files
    known = uow.archive.downloads() if known is None else known
    queued = {(d.team_id, d.target) for d in uow.run.downloads}
    jobs = []
    for file in referenced:
        key = (team_id, file["id"])
        url = file.get("url_private_download") or file.get("url_private")
        held = stored.get(file["id"])
        prior = known.get(key)
        if (
            file.get("mode") in UNDOWNLOADABLE_MODES
            or not url
            or key in queued
            or (held is not None and held.blob is not None)
            or (prior is not None and prior.status in TERMINAL_STATUSES)
        ):
            continue
        jobs.append(DownloadJob(team_id, "file", file["id"], url, max_bytes, slack_auth=True))
        uow.run.downloads.append(
            DownloadState(
                team_id=team_id,
                target_kind="file",
                target=file["id"],
                url=url,
                status="pending",
                attempts=prior.attempts if prior is not None else 0,
            )
        )
        queued.add(key)
    return jobs
