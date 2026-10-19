# ABOUTME: Checks the archive recorded from the test workspace: no secrets, scrubbed emails and
# ABOUTME: the message shapes later tasks test against.
import re

from tests.recorded_fixtures import (
    RECORDED,
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
