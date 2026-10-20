# ABOUTME: Readers for the archive recorded from the test workspace in tests/fixtures/workspace/.
# ABOUTME: Returns raw lines, newest directory objects, a DirectoryView, blobs, a copy or a lake.
import atexit
import json
import os
import shutil
import tempfile
from functools import cache
from pathlib import Path

from slarc.adapters.jsonl_import import import_jsonl
from slarc.adapters.lake import extension_directory
from slarc.domain.directory import DirectoryView
from tests.fakes import InMemoryBlobStore

RECORDED = Path(__file__).parent / "fixtures" / "workspace"
RECORDED_TEAM = "T0CB5LYPVFA"


def recorded_lines(pattern: str) -> list[dict]:
    """Every line of the recorded files matching a glob pattern relative to the archive root."""
    return [
        json.loads(line)
        for path in sorted(RECORDED.glob(pattern))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def recorded_messages(channel: str = "*") -> list[dict]:
    lines = recorded_lines(f"raw/{RECORDED_TEAM}/{channel}/*.jsonl")
    return [line["object"] for line in lines if line["meta"]["kind"] == "message"]


def recorded_directory(name: str) -> dict[str, dict]:
    """The newest object per id in a directory file such as users.jsonl."""
    lines = recorded_lines(f"directory/{RECORDED_TEAM}/{name}")
    return {line["meta"]["id"]: line["object"] for line in lines}


def copy_recorded(target: Path) -> Path:
    """A copy of the recorded archive, without its blobs, at target."""
    shutil.copytree(RECORDED, target, ignore=shutil.ignore_patterns("blobs"))
    return target


def recorded_lake(target: Path) -> Path:
    """A copy of the recorded archive, without its blobs, imported into a lake at target."""
    shutil.copytree(_recorded_lake_template(), target)
    return target


@cache
def _recorded_lake_template() -> Path:
    """The recorded archive imported once per test session, for recorded_lake to copy."""
    parent = Path(tempfile.mkdtemp(prefix="slarc-recorded-lake-"))
    atexit.register(shutil.rmtree, parent, ignore_errors=True)
    root = copy_recorded(parent / "archive")
    import_jsonl(root, extension_directory(os.environ, Path.home()))
    return root


def recorded_blobs() -> InMemoryBlobStore:
    """The recorded blobs, which leave out those over 1 MiB, in an in-memory blob store."""
    blobs = InMemoryBlobStore()
    for path in sorted((RECORDED / "blobs").rglob("*")):
        if path.is_file():
            blobs.write([path.read_bytes()])
    return blobs


def recorded_view() -> DirectoryView:
    """A DirectoryView over the recorded directory files."""
    return DirectoryView(
        users=recorded_directory("users.jsonl"),
        conversations=recorded_directory("conversations.jsonl"),
        usergroups=recorded_directory("usergroups.jsonl"),
        emoji={name: obj.get("url", "") for name, obj in recorded_directory("emoji.jsonl").items()},
        bots=recorded_directory("bots.jsonl"),
    )
