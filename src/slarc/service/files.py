# ABOUTME: Reads one archived file's bytes, named by its id or its Slack link, for slarc file.
# ABOUTME: A file the archive knows but never downloaded raises FileNotArchived with the reason.
from slarc.domain.links import parse_file_selector
from slarc.ports import BlobStore, QueryIndex, SlarcError
from slarc.service.listing import workspace_domains

# Why a file has no copy, by its download state's status.
REASONS = {
    "pending": "slarc pull has not downloaded it yet",
    "failed": "its download failed",
    "gave_up": "its download failed too many times",
    "forbidden": "Slack refused the download",
    "not_found": "Slack no longer has it",
    "too_large": "it is larger than the download size limit",
    "deleted": "it was deleted from Slack before slarc saved it",
    "external": "it is stored outside Slack",
}


class FileNotArchived(SlarcError):
    """The archive knows the file but holds no copy of it."""


def file_bytes(index: QueryIndex, blobs: BlobStore, selector: str) -> tuple[str, bytes]:
    """The name and bytes of the file a file id or a file's Slack link names."""
    try:
        file_id, domain = parse_file_selector(selector)
    except ValueError:
        raise SlarcError(
            "slarc file takes a file id such as F0123456789 or a file's Slack link, "
            f"not '{selector}'."
        ) from None
    team = None
    if domain is not None:
        teams = {name: team_id for team_id, name in workspace_domains(index).items()}
        team = teams.get(domain)
        if team is None:
            held = ", ".join(sorted(teams)) or "none"
            raise SlarcError(f"This archive holds no workspace {domain}. Workspaces here: {held}.")
    record = index.file_record(file_id, team)
    if record is None:
        raise SlarcError(f"No file {file_id} is in the archive.")
    if record.blob is None or not blobs.exists(record.blob):
        reason = REASONS.get(record.status or "", "slarc holds no copy of it")
        if record.last_error:
            reason += f" ({record.last_error})"
        raise FileNotArchived(f"{file_id} is not in the archive: {reason}.")
    return record.name, blobs.read(record.blob)
