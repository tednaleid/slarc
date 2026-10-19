#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "slarc",
# ]
#
# [tool.uv.sources]
# slarc = { path = "..", editable = true }
# ///
# ABOUTME: Developer-only script that pulls the naleidworkspace test workspace with slarc's pull
# ABOUTME: service into tests/fixtures/workspace/, scrubbing emails and dropping large blobs.
import re
import shutil
import sys
import tempfile
from pathlib import Path

import msgspec

from slarc import __version__
from slarc.adapters.desktop_session import DesktopSessionSource
from slarc.adapters.slack_gateway import SessionConnector
from slarc.bootstrap import SystemClock, archive_stores, http_downloader
from slarc.config import create_archive
from slarc.domain.envelope import decode_line, encode_line
from slarc.domain.hashing import content_hash
from slarc.domain.links import ConversationSel, Selector
from slarc.ports import Session
from slarc.service.pull import PullOptions, pull
from slarc.unit_of_work import UnitOfWork

TEAM_ID = "T0CB5LYPVFA"
DOMAIN = "naleidworkspace"
CONVERSATIONS = ("C0CA5396G03", "C0CA7SNHTSP", "D0CAD7JE1QR", "C0CAG251117")
ARCHIVED_CHANNEL = "slarc-fixtures-archived"
TARGET = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "workspace"
KEPT = ("README.md",)
MAX_BLOB_BYTES = 1024 * 1024
EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@(?!example\.invalid\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SCRUBBED_DOMAIN = "example.invalid"
SECRET_MARK = b"xox"


class TestWorkspaceSessions:
    """The desktop sessions narrowed to the test workspace, so no other token is reachable."""

    def sessions(self) -> list[Session]:
        desktop = DesktopSessionSource(Path.home(), sys.platform).sessions()
        return [session for session in desktop if session.team_id == TEAM_ID]


class PrintReporter:
    def info(self, msg: str) -> None:
        print(msg)

    def warn(self, msg: str) -> None:
        print(f"Warning: {msg}")

    def progress(self, label: str, done: int, total: int | None) -> None:
        if total is not None:
            print(f"{label}: {done} messages")


def selectors() -> list[Selector]:
    by_id = [ConversationSel(DOMAIN, channel, None, None) for channel in CONVERSATIONS]
    return [*by_id, ConversationSel(DOMAIN, None, ARCHIVED_CHANNEL, None)]


def record(root: Path) -> None:
    create_archive(root)
    connector = SessionConnector(TestWorkspaceSessions())
    args = [*CONVERSATIONS, f"#{ARCHIVED_CHANNEL}"]
    with (
        archive_stores(root) as (archive, blobs),
        http_downloader(blobs, connector.authenticated) as downloader,
        UnitOfWork(archive, blobs, SystemClock(), "pull", args, __version__) as uow,
    ):
        report = pull(selectors(), PullOptions(), uow, connector, downloader, PrintReporter())
        if uow.run.teams != [TEAM_ID]:
            raise SystemExit(f"The pull reached workspaces other than {TEAM_ID}.")
    print(f"pulled {report.items_pulled} items, {report.lines_appended} lines")


def scrub_line(line: bytes) -> bytes:
    """The line with emails moved to example.invalid and its content hash recomputed."""
    text = line.decode()
    scrubbed = EMAIL.sub(rf"\1@{SCRUBBED_DOMAIN}", text)
    if scrubbed == text:
        return line
    env = decode_line(scrubbed.encode())
    if env.meta.hash is not None and env.object is not None:
        meta = msgspec.structs.replace(env.meta, hash=content_hash(env.meta.kind, env.object))
        env = msgspec.structs.replace(env, meta=meta)
    return encode_line(env)


def copy_scrubbed(source: Path, target: Path) -> list[Path]:
    """Copies the archive, returning the blobs left out for size."""
    dropped = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        out = target / relative
        if relative.parts[0] == "blobs" and path.stat().st_size > MAX_BLOB_BYTES:
            dropped.append(relative)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".jsonl":
            lines = path.read_bytes().splitlines(keepends=True)
            out.write_bytes(b"".join(scrub_line(line) for line in lines))
        else:
            shutil.copyfile(path, out)
    return dropped


def clear(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for entry in target.iterdir():
        if entry.name in KEPT:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def files_with_secret_mark(target: Path) -> list[Path]:
    return [p for p in sorted(target.rglob("*")) if p.is_file() and SECRET_MARK in p.read_bytes()]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "archive"
        record(root)
        clear(TARGET)
        dropped = copy_scrubbed(root, TARGET)
    for relative in dropped:
        print(f"left out blob over 1 MB: {relative}")
    marked = files_with_secret_mark(TARGET)
    if marked:
        clear(TARGET)
        names = ", ".join(str(p.relative_to(TARGET)) for p in marked)
        print(f"Removed the recorded fixtures: these files contain 'xox': {names}")
        return 1
    print(f"recorded fixtures in {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
