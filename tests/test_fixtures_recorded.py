# ABOUTME: Checks the archive recorded from the test workspace: no secrets, scrubbed emails, the
# ABOUTME: message shapes later tasks test against, and the lake the test loader builds from it.
import re
from pathlib import Path

import msgspec

from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.tracking import DownloadState, TrackedItem
from tests.fakes import FakeArchiveLock
from tests.jsonl_loader import load_jsonl
from tests.recorded_fixtures import (
    RECORDED,
    RECORDED_TEAM,
    recorded_directory,
    recorded_lines,
    recorded_messages,
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JIM = "U0CA61XJF2R"
FIXTURES_CHANNEL = "C0CA7SNHTSP"
GROUP_DM = "C0CAG251117"
CANVAS = "F0CA8RBM16Z"


def test_fixture_archive_has_no_secrets():
    files = [path for path in RECORDED.rglob("*") if path.is_file()]
    assert len(files) > 10
    assert [path for path in files if b"xox" in path.read_bytes()] == []


def test_fixture_emails_scrubbed():
    texts = [path.read_text(encoding="utf-8") for path in RECORDED.rglob("*.jsonl")]
    emails = {email for text in texts for email in EMAIL.findall(text)}
    assert emails
    assert {email for email in emails if not email.endswith("@example.invalid")} == set()
    assert recorded_directory("users.jsonl")[JIM]["profile"]["email"].endswith("@example.invalid")


def test_fixture_covers_expected_subtypes():
    messages = recorded_messages()
    subtypes = {m.get("subtype") for m in messages}
    assert {"thread_broadcast", "tombstone", "channel_topic", "huddle_thread"} <= subtypes
    assert any(block["type"] == "image" for m in messages for block in m.get("blocks") or [])
    assert any(m.get("attachments") for m in messages)
    assert any(f.get("mode") == "snippet" for f in recorded_directory("files.jsonl").values())

    reactions = {r["name"] for m in messages for r in m.get("reactions") or []}
    assert any("::skin-tone-" in name for name in reactions)
    assert "teamwork" in reactions
    assert any(":teamwork2:" in m.get("text", "") for m in messages)
    emoji = recorded_directory("emoji.jsonl")
    assert emoji["teamwork"]["url"].startswith("https://")
    assert emoji["teamwork2"]["url"] == "alias:teamwork"

    conversations = recorded_directory("conversations.jsonl")
    tabs = conversations[FIXTURES_CHANNEL]["properties"]["tabs"]
    assert CANVAS in {(tab.get("data") or {}).get("file_id") for tab in tabs}
    assert conversations[GROUP_DM]["is_mpim"]
    assert recorded_messages(GROUP_DM)

    jim = recorded_directory("users.jsonl")[JIM]
    assert (jim["real_name"], jim["profile"]["display_name"]) == ("Jim Naleid", "Jim")
    assert [run["meta"]["kind"] for run in recorded_lines("runs.jsonl")] == ["run"]


def test_loader_reproduces_the_fixture(tmp_path: Path, ext_dir: Path):
    load_jsonl(RECORDED, tmp_path, ext_dir)

    counts = {
        path.parent.name: len(path.read_bytes().splitlines())
        for path in RECORDED.glob(f"raw/{RECORDED_TEAM}/*/*.jsonl")
    }
    run = recorded_lines("runs.jsonl")[-1]["object"]
    tracked = [msgspec.convert(item, TrackedItem) for item in run["tracked"]]
    blobbed = {
        line["meta"]["id"]
        for line in recorded_lines(f"directory/{RECORDED_TEAM}/*.jsonl")
        if line["meta"].get("blob")
    }
    downloads = [
        msgspec.convert(state, DownloadState)
        for state in run["downloads"]
        if state["target"] not in blobbed
    ]
    assert len(counts) == 5
    assert downloads

    store = DuckLakeArchiveStore(tmp_path, ext_dir, FakeArchiveLock())
    try:
        channels = store.channels(RECORDED_TEAM)
        assert {c: len(list(store.message_lines(RECORDED_TEAM, c))) for c in channels} == counts
        assert {**store.tracked_items(), **store.removed_items()} == {
            item.key: msgspec.structs.replace(item, revision=1) for item in tracked
        }
        assert store.downloads() == {(s.team_id, s.target): s for s in downloads}
    finally:
        store.close()
