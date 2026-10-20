# ABOUTME: Stamps and appends archive lines for the current run, counting each one appended.
# ABOUTME: Directory lines are appended only when their hash or blob differs from the stored line.
import msgspec

from slarc import __version__
from slarc.domain.directory import directory_image_url
from slarc.domain.envelope import Envelope, Kind, Meta, fetched_at
from slarc.domain.hashing import content_hash
from slarc.ports import DirectoryState
from slarc.unit_of_work import UnitOfWork

LINES_APPENDED = "lines_appended"


def append_directory(
    uow: UnitOfWork,
    team_id: str,
    kind: Kind,
    objects: dict[str, dict],
    api: str,
    *,
    keep_blob: bool = False,
) -> dict[str, DirectoryState]:
    """Appends each object whose hash or blob differs from its stored line; returns their state.

    keep_blob keeps the stored blob of an object whose hash is unchanged, so it is not
    appended again without it; a changed object's line has no blob, so its bytes are
    downloaded again. A user or emoji line carries the blob of its image once that image is
    downloaded.
    """
    appended: dict[str, DirectoryState] = {}
    if not objects:
        return appended
    state = uow.archive.directory_state(team_id, kind)
    images = uow.archive.directory_state(team_id, "image") if kind in ("user", "emoji") else {}
    for id_, obj in objects.items():
        digest = content_hash(kind, obj)
        held = state.get(id_)
        if keep_blob:
            blob = held.blob if held is not None and held.hash == digest else None
        else:
            image = images.get(directory_image_url(kind, obj) or "")
            blob = image.blob if image is not None else None
        if held is not None and (held.hash, held.blob) == (digest, blob):
            continue
        meta = stamped_meta(uow, kind, team=team_id, id=id_, api=api, hash=digest, blob=blob)
        append_line(uow, Envelope(meta=meta, object=obj))
        appended[id_] = DirectoryState(hash=digest, blob=blob)
    return appended


def fetched_now(uow: UnitOfWork) -> str:
    return fetched_at(uow.clock.now())


def stamped_meta(uow: UnitOfWork, kind: Kind, **fields: str | None) -> Meta:
    stamped = Meta(kind=kind, fetched_at=fetched_now(uow), run=uow.run.run, slarc=__version__)
    return msgspec.structs.replace(stamped, **fields)


def append_line(uow: UnitOfWork, env: Envelope) -> None:
    uow.archive.append(env)
    count(uow, LINES_APPENDED)


def count(uow: UnitOfWork, name: str) -> None:
    uow.run.counts[name] = counted(uow, name) + 1


def counted(uow: UnitOfWork, name: str) -> int:
    """The run's count under name, or 0 when nothing is counted there."""
    value = uow.run.counts.get(name, 0)
    return int(value) if isinstance(value, int | float) else 0
