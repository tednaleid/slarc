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
# ABOUTME: Live check of slarc's acceptance criteria against the naleidworkspace test workspace.
# ABOUTME: Pulls into scratch archives; edits and first-run posts go through seed-test-workspace.py.
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from slarc.adapters.lake import connect_lake, extension_directory
from slarc.adapters.lake_store import VERSION_ORDER

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "scripts" / "seed-test-workspace.py"
WORKSPACE = "naleidworkspace"
TEAM_ID = "T0CB5LYPVFA"
FIXTURES = "C0CA7SNHTSP"
ALL_NALEID = "C0CA5396G03"
SEARCH_WAIT_S = 120
RETRY_PAUSE_S = 10
FIXTURES_TEXT = "seed: edited message, edited text"
SAME_PULL = "(a) pulling the same channels twice adds no message or directory rows"
EXPORT = "(d) the export shows seeded messages, giphy, a link preview, snippets, a deleted file"
REPLY = "(b) a new reply under an older parent lands as exactly one new line"
EDIT = "(c) an edit lands as exactly one new line by pull --full"


def link(channel: str) -> str:
    return f"https://{WORKSPACE}.slack.com/archives/{channel}"


@dataclass
class Criterion:
    name: str
    passed: bool = True
    notes: list[str] = field(default_factory=list)
    timings: list[str] = field(default_factory=list)

    def check(self, ok: bool, message: str) -> None:
        if not ok:
            self.passed = False
        self.notes.append(("ok: " if ok else "FAILED: ") + message)


class Timed:
    """Runs commands, recording how long each took under a label."""

    def __init__(self, criterion: Criterion) -> None:
        self.criterion = criterion

    def run(self, label: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        done = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
        self.criterion.timings.append(f"{label} {time.monotonic() - started:.1f}s")
        if done.returncode != 0:
            raise RuntimeError(f"{label} exited {done.returncode}: {done.stderr.strip()}")
        return done


def slarc(timed: Timed, label: str, root: Path, command: str, *args: str) -> None:
    timed.run(label, ["uv", "run", "slarc", command, "--root", str(root), *args])


def seed(timed: Timed, label: str, *args: str) -> dict:
    done = timed.run(label, [str(SEED), "--workspace", WORKSPACE, *args])
    return json.loads(done.stdout.strip().splitlines()[-1])


def lake_rows(root: Path, query: str, params: list | None = None) -> list[tuple]:
    """The rows of a query over the archive's lake tables."""
    con = connect_lake(root, extension_directory(os.environ, Path.home()), read_only=True)
    try:
        return con.execute(query, params or []).fetchall()
    finally:
        con.close()


def last_run(root: Path) -> dict:
    rows = lake_rows(root, f"SELECT object FROM run ORDER BY {VERSION_ORDER}")
    return json.loads(rows[-1][0])


def run_summary(root: Path) -> str:
    counts = last_run(root)["counts"]
    seen = int(counts.get("messages_seen", 0))
    appended = int(counts.get("lines_appended", 0))
    wait = counts.get("rate_limit_wait_s")
    waited = "not recorded" if wait is None else f"{wait:.1f}s"
    return f"messages_seen={seen} lines_appended={appended} rate_limit_wait={waited}"


def row_counts(root: Path) -> dict[str, int]:
    return {
        table: lake_rows(root, f"SELECT count(*) FROM {table}")[0][0]
        for table in ("message_version", "directory_entry")
    }


def channel_rows(root: Path, channel: str) -> list[tuple[str, str | None]]:
    """The (ts, text) of every message_version row of the channel, oldest version first."""
    query = (
        "SELECT ts, object ->> 'text' FROM message_version WHERE team = ? AND channel = ? "
        f"ORDER BY {VERSION_ORDER}"
    )
    return lake_rows(root, query, [TEAM_ID, channel])


def check_same_pull_twice(root: Path) -> Criterion:
    criterion = Criterion(SAME_PULL)
    timed = Timed(criterion)
    links = [link(FIXTURES), link(ALL_NALEID)]
    slarc(timed, "first pull", root, "pull", "--yes", *links)
    criterion.notes.append(f"first pull: {run_summary(root)}")
    for name, channel in (("#slarc-fixtures", FIXTURES), ("#all-naleid", ALL_NALEID)):
        stored = len(channel_rows(root, channel))
        criterion.check(stored > 0, f"{stored} message rows stored for {name}")
    before = row_counts(root)
    slarc(timed, "second pull", root, "pull", *links)
    criterion.notes.append(f"second pull: {run_summary(root)}")
    after = row_counts(root)
    criterion.check(after == before, f"row counts before {before}, after {after}")
    return criterion


def check_export(root: Path, out: Path) -> Criterion:
    criterion = Criterion(EXPORT)
    timed = Timed(criterion)
    slarc(timed, "export", root, "export", FIXTURES, ALL_NALEID, "--out", str(out))
    fixtures_page = (out / f"{FIXTURES}.html").read_text()
    criterion.check(FIXTURES_TEXT in fixtures_page, f"#slarc-fixtures page shows {FIXTURES_TEXT!r}")
    page = (out / f"{ALL_NALEID}.html").read_text()
    giphy = re.search(r'<figure class="image"><img src="(assets/[0-9a-f]{64}\.gif)"', page)
    has_giphy = giphy is not None and (out / giphy.group(1)).is_file()
    criterion.check(has_giphy and "Posted using /giphy" in page, "giphy GIF from a local asset")
    criterion.check("tednaleid - Overview" in page, "link preview title 'tednaleid - Overview'")
    criterion.check(
        "10k-snippet.txt" in page and "test snippet line 10000" in page,
        "10k-snippet.txt with its last line",
    )
    criterion.check(
        "test-snippet-2.txt" in page and len(re.findall(r"test snippet line 600\b", page)) == 2,
        "test-snippet-2.txt with its last line (line 600 appears in both snippets)",
    )
    criterion.check("This file was deleted." in page, "deleted file placeholder")
    return criterion


def pull_until(timed: Timed, root: Path, found: Callable[[], bool]) -> tuple[int, float]:
    """Bare-pulls until found() or SEARCH_WAIT_S passes; returns the pulls made and seconds.

    Search indexes a reply some seconds after it is posted, so the run that posts R may need
    more than one pull; later runs find R on the first.
    """
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        slarc(timed, f"bare pull {attempts}", root, "pull")
        elapsed = time.monotonic() - started
        if found() or elapsed >= SEARCH_WAIT_S:
            return attempts, elapsed
        time.sleep(RETRY_PAUSE_S)


def forget_reply(root: Path, channel: str, reply_ts: str) -> tuple[int, int]:
    """Edits the archive into the state a pull leaves when it runs just before R is posted.

    That pull stores P (the hashed subset has no reply_count, so P's row is the same) but not
    R, and its last_search_at precedes R. Deleting R's message_version rows and moving the
    channel's tracked_item last_search_at to a second before R gives that state. Returns the
    rows deleted and the tracked items moved.
    """
    before_reply = datetime.fromtimestamp(int(reply_ts.split(".")[0]) - 1, UTC)
    con = connect_lake(root, extension_directory(os.environ, Path.home()))
    try:
        con.execute("BEGIN")
        removed = con.execute(
            "DELETE FROM message_version WHERE team = ? AND channel = ? AND ts = ?",
            [TEAM_ID, channel, reply_ts],
        ).fetchall()[0][0]
        moved = con.execute(
            "UPDATE tracked_item SET last_search_at = ? "
            "WHERE team = ? AND kind = 'conversation' AND channel = ?",
            [before_reply.strftime("%Y-%m-%dT%H:%M:%SZ"), TEAM_ID, channel],
        ).fetchall()[0][0]
        con.execute("COMMIT")
    finally:
        con.close()
    return removed, moved


def check_reply_under_older_parent(root: Path) -> tuple[Criterion, dict]:
    criterion = Criterion(REPLY)
    timed = Timed(criterion)
    fixed = seed(timed, "ensure P, R and E", "ensure-acceptance")
    criterion.notes.append(
        f"ensure-acceptance posted {fixed['posted']} messages; #slarc-acceptance held "
        f"{fixed['messages_before']} before it"
    )
    channel, reply = fixed["channel"], fixed["reply"]
    root.mkdir(parents=True)
    # No thread is active, so only search can find R.
    (root / "slarc.toml").write_text("[pull]\nactive_thread_days = 0\n")
    slarc(timed, "first pull", root, "pull", link(channel))
    removed, moved = forget_reply(root, channel, reply)
    criterion.check(removed == 1, f"removed {removed} row for R from the archive")
    criterion.check(moved == 1, f"moved last_search_at before R for {moved} tracked item")
    stored = len(channel_rows(root, channel))

    def new_rows() -> list[tuple[str, str | None]]:
        return channel_rows(root, channel)[stored:]

    attempts, elapsed = pull_until(timed, root, lambda: bool(new_rows()))
    rows = new_rows()
    criterion.notes.append(f"{attempts} bare pulls over {elapsed:.0f}s; last: {run_summary(root)}")
    criterion.check(len(rows) == 1, f"{len(rows)} new rows in the channel")
    criterion.check([ts for ts, _ in rows] == [reply], f"the new row has R's ts {reply}")
    searched = last_run(root)["counts"].get("threads_read", {}).get("search", 0)
    criterion.check(searched >= 1, f"threads_read.search={searched} in the last pull's run line")
    return criterion, fixed


def check_edit(root: Path, fixed: dict, tag: str) -> Criterion:
    criterion = Criterion(EDIT)
    timed = Timed(criterion)
    channel, target = fixed["channel"], fixed["edit_target"]
    edited = f"{fixed['edit_marker']}, edited by run {tag}"

    def rows_for_target() -> list[tuple[str, str | None]]:
        return [row for row in channel_rows(root, channel) if row[0] == target]

    stored = len(rows_for_target())
    criterion.check(stored == 1, f"{stored} row holds E before the edit")
    seed(timed, "seed edit", "edit", target, edited)
    slarc(timed, "bare pull", root, "pull")
    after_bare = len(rows_for_target()) - stored
    criterion.notes.append(f"bare pull after the edit: {after_bare} new rows; {run_summary(root)}")
    slarc(timed, "pull --full", root, "pull", "--full")
    criterion.notes.append(f"pull --full: {run_summary(root)}")
    new = rows_for_target()[stored:]
    criterion.check(len(new) == 1, f"{len(new)} new rows for E across both pulls")
    text = new[-1][1] if new else None
    criterion.check(text == edited, f"the new row has the edited text: {text!r}")
    return criterion


def report(criterion: Criterion) -> None:
    status = "PASS" if criterion.passed else "FAIL"
    print(f"{status} {criterion.name} [{', '.join(criterion.timings)}]", flush=True)
    for note in criterion.notes:
        print(f"    {note}", flush=True)


def attempt(name: str, check: Callable[[], Criterion]) -> Criterion:
    """Runs a check, reporting it with its total time; an exception fails it."""
    started = time.monotonic()
    try:
        criterion = check()
    except Exception as error:
        criterion = Criterion(name)
        criterion.check(False, f"{type(error).__name__}: {error}")
    criterion.timings.append(f"total {time.monotonic() - started:.1f}s")
    report(criterion)
    return criterion


def main() -> int:
    tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    tmp = Path(tempfile.mkdtemp(prefix="slarc-acceptance-"))
    fixtures, acceptance = tmp / "fixtures", tmp / "acceptance"
    results = [
        attempt(SAME_PULL, lambda: check_same_pull_twice(fixtures)),
        attempt(EXPORT, lambda: check_export(fixtures, tmp / "export")),
    ]
    fixed: list[dict] = []

    def reply_under_older_parent() -> Criterion:
        criterion, ensured = check_reply_under_older_parent(acceptance)
        fixed.append(ensured)
        return criterion

    results.append(attempt(REPLY, reply_under_older_parent))
    if fixed:
        results.append(attempt(EDIT, lambda: check_edit(acceptance, fixed[0], tag)))
    else:
        results.append(attempt(EDIT, lambda: Criterion(f"{EDIT}: skipped, (b) failed", False)))
    if all(c.passed for c in results):
        shutil.rmtree(tmp)
        return 0
    print(f"Scratch archives kept for inspection in {tmp}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
