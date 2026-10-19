#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
# ABOUTME: Live check of slarc's Phase 1 ship criteria against the naleidworkspace test workspace.
# ABOUTME: Pulls into scratch archives; edits and first-run posts go through seed-test-workspace.py.
import hashlib
import json
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

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "scripts" / "seed-test-workspace.py"
WORKSPACE = "naleidworkspace"
TEAM_ID = "T0CB5LYPVFA"
FIXTURES = "C0CA7SNHTSP"
ALL_NALEID = "C0CA5396G03"
SEARCH_WAIT_S = 120
RETRY_PAUSE_S = 10
FIXTURES_TEXT = "seed: edited message, edited text"
SAME_PULL = "(a) pulling the same channels twice changes only runs.jsonl"
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


def last_run(root: Path) -> dict:
    lines = (root / "runs.jsonl").read_text().splitlines()
    return json.loads(lines[-1])["object"]


def run_summary(root: Path) -> str:
    counts = last_run(root)["counts"]
    seen = int(counts.get("messages_seen", 0))
    appended = int(counts.get("lines_appended", 0))
    wait = counts.get("rate_limit_wait_s")
    waited = "not recorded" if wait is None else f"{wait:.1f}s"
    return f"messages_seen={seen} lines_appended={appended} rate_limit_wait={waited}"


def file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def channel_lines(root: Path, channel: str) -> list[dict]:
    """Every line of the channel's message files, oldest file first, in file order."""
    lines = []
    for path in sorted((root / "raw" / TEAM_ID / channel).glob("*.jsonl")):
        lines += [json.loads(line) for line in path.read_text().splitlines()]
    return lines


def check_same_pull_twice(root: Path) -> Criterion:
    criterion = Criterion(SAME_PULL)
    timed = Timed(criterion)
    links = [link(FIXTURES), link(ALL_NALEID)]
    slarc(timed, "first pull", root, "pull", "--yes", *links)
    criterion.notes.append(f"first pull: {run_summary(root)}")
    for name, channel in (("#slarc-fixtures", FIXTURES), ("#all-naleid", ALL_NALEID)):
        stored = len(channel_lines(root, channel))
        criterion.check(stored > 0, f"{stored} message lines stored for {name}")
    before = file_hashes(root)
    slarc(timed, "second pull", root, "pull", *links)
    criterion.notes.append(f"second pull: {run_summary(root)}")
    after = file_hashes(root)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    criterion.check(changed == ["runs.jsonl"], f"changed files: {changed}")
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

    That pull stores P (the hashed subset has no reply_count, so P's line is the same) but not
    R, and its last_search_at precedes R. Removing R's line and moving the channel's
    last_search_at, held in the tracked state of the only run line, to a second before R gives
    that state. Returns the lines removed and the tracked items moved.
    """
    removed = 0
    for path in sorted((root / "raw" / TEAM_ID / channel).glob("*.jsonl")):
        lines = path.read_text().splitlines(keepends=True)
        kept = [line for line in lines if json.loads(line)["meta"].get("ts") != reply_ts]
        removed += len(lines) - len(kept)
        path.write_text("".join(kept))
    runs = root / "runs.jsonl"
    run_lines = runs.read_text().splitlines()
    run = json.loads(run_lines[-1])
    before_reply = datetime.fromtimestamp(int(reply_ts.split(".")[0]) - 1, UTC)
    moved = 0
    for item in run["object"]["tracked"]:
        if item["kind"] == "conversation" and item["channel"] == channel:
            item["last_search_at"] = before_reply.strftime("%Y-%m-%dT%H:%M:%SZ")
            moved += 1
    run_lines[-1] = json.dumps(run, separators=(",", ":"), ensure_ascii=False)
    runs.write_text("\n".join(run_lines) + "\n")
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
    criterion.check(removed == 1, f"removed {removed} line for R from the archive")
    criterion.check(moved == 1, f"moved last_search_at before R for {moved} tracked item")
    stored = len(channel_lines(root, channel))

    def new_lines() -> list[dict]:
        return channel_lines(root, channel)[stored:]

    attempts, elapsed = pull_until(timed, root, lambda: bool(new_lines()))
    lines = new_lines()
    criterion.notes.append(f"{attempts} bare pulls over {elapsed:.0f}s; last: {run_summary(root)}")
    criterion.check(len(lines) == 1, f"{len(lines)} new lines in the channel")
    criterion.check(
        [line["meta"].get("ts") for line in lines] == [reply],
        f"the new line has R's ts {reply}",
    )
    searched = last_run(root)["counts"].get("threads_read", {}).get("search", 0)
    criterion.check(searched >= 1, f"threads_read.search={searched} in the last pull's run line")
    return criterion, fixed


def check_edit(root: Path, fixed: dict, tag: str) -> Criterion:
    criterion = Criterion(EDIT)
    timed = Timed(criterion)
    channel, target = fixed["channel"], fixed["edit_target"]
    edited = f"{fixed['edit_marker']}, edited by run {tag}"

    def lines_for_target() -> list[dict]:
        return [line for line in channel_lines(root, channel) if line["meta"].get("ts") == target]

    stored = len(lines_for_target())
    criterion.check(stored == 1, f"{stored} line holds E before the edit")
    seed(timed, "seed edit", "edit", target, edited)
    slarc(timed, "bare pull", root, "pull")
    after_bare = len(lines_for_target()) - stored
    criterion.notes.append(f"bare pull after the edit: {after_bare} new lines; {run_summary(root)}")
    slarc(timed, "pull --full", root, "pull", "--full")
    criterion.notes.append(f"pull --full: {run_summary(root)}")
    new = lines_for_target()[stored:]
    criterion.check(len(new) == 1, f"{len(new)} new lines for E across both pulls")
    text = new[-1]["object"].get("text") if new else None
    criterion.check(text == edited, f"the new line has the edited text: {text!r}")
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
