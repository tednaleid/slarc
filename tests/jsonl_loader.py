# ABOUTME: Test-only loader that builds a lake from a JSONL archive such as the recorded fixture,
# ABOUTME: appending its lines and committing the state its run lines describe through the store.
from pathlib import Path

import msgspec

from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.envelope import Envelope
from slarc.domain.tracking import DownloadState, ItemKey, TrackedItem, run_from_envelope
from slarc.ports import Commit, DownloadKey
from tests.fakes import FakeArchiveLock

_decoder = msgspec.json.Decoder(Envelope)


def jsonl_envelopes(source: Path, pattern: str) -> list[Envelope]:
    """Every line of the files under source matching a glob pattern, in path order."""
    return [
        _decoder.decode(line)
        for path in sorted(source.glob(pattern))
        for line in path.read_bytes().splitlines()
        if line.strip()
    ]


def load_jsonl(source: Path, root: Path, ext_dir: Path) -> None:
    """Builds a lake at root from the JSONL archive at source.

    The tracked items and download states are the latest the run lines name, in file order;
    a download state whose target has a blob is left out, as it is no longer outstanding.
    """
    messages = jsonl_envelopes(source, "raw/*/*/*.jsonl")
    directory = jsonl_envelopes(source, "directory/*/*.jsonl")
    runs = jsonl_envelopes(source, "runs.jsonl")
    tracked: dict[ItemKey, TrackedItem] = {}
    downloads: dict[DownloadKey, DownloadState] = {}
    for run in map(run_from_envelope, runs):
        tracked.update((item.key, item) for item in run.tracked)
        downloads.update(((state.team_id, state.target), state) for state in run.downloads)
    blobbed = {(env.meta.team, env.meta.id) for env in directory if env.meta.blob}
    store = DuckLakeArchiveStore(root, ext_dir, FakeArchiveLock())
    try:
        for env in [*messages, *directory]:
            store.append(env)
        store.commit(
            Commit(
                items=[msgspec.structs.replace(item, revision=0) for item in tracked.values()],
                downloads=[state for key, state in downloads.items() if key not in blobbed],
            )
        )
        for env in runs:
            store.append_run(run_from_envelope(env), env.meta.slarc or "")
    finally:
        store.close()
