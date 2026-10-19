# ABOUTME: Readers for the archive recorded from the test workspace in tests/fixtures/workspace/.
# ABOUTME: Returns raw lines, the newest directory object per id and a DirectoryView, or a copy.
import json
import shutil
from pathlib import Path

from slarc.domain.directory import DirectoryView

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


def recorded_view() -> DirectoryView:
    """A DirectoryView over the recorded directory files."""
    return DirectoryView(
        users=recorded_directory("users.jsonl"),
        conversations=recorded_directory("conversations.jsonl"),
        usergroups=recorded_directory("usergroups.jsonl"),
        emoji={name: obj.get("url", "") for name, obj in recorded_directory("emoji.jsonl").items()},
        bots=recorded_directory("bots.jsonl"),
    )
