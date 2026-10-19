#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "slack-sdk",
#     "slarc",
# ]
#
# [tool.uv.sources]
# slarc = { path = "..", editable = true }
# ///
# ABOUTME: Developer-only script that posts test content to the naleidworkspace test workspace.
# ABOUTME: Aborts on any other workspace and skips content an earlier run already posted.
import argparse
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from slarc.adapters.desktop_session import DesktopSessionSource
from slarc.adapters.slack_gateway import web_api

TEAM_ID = "T0CB5LYPVFA"
MARKER = "slarc seed v1"
ARCHIVED_CHANNEL = "slarc-fixtures-archived"
JIM = "U0CA61XJF2R"
TOPIC = "Fixtures for slarc tests"
PURPOSE = "Seeded content that slarc's recorded fixtures are pulled from"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
IGNORED_ERRORS = ("already_reacted", "already_pinned")


def pages(call: Callable[..., dict], key: str, **params: object) -> Iterator[dict]:
    cursor = None
    while True:
        response = call(**params, cursor=cursor, limit=200)
        yield from response[key]
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


class Conversation:
    """A conversation and the texts of every message in it, read once at start."""

    def __init__(self, client: WebClient, channel_id: str) -> None:
        self.client = client
        self.id = channel_id
        self.texts: dict[str, str] = {}
        for message in pages(client.conversations_history, "messages", channel=channel_id):
            self.texts[message["ts"]] = message.get("text", "")
            if (message.get("reply_count") or 0) > 0:
                replies = pages(
                    client.conversations_replies, "messages", channel=channel_id, ts=message["ts"]
                )
                for reply in replies:
                    self.texts[reply["ts"]] = reply.get("text", "")

    def find(self, key: str) -> str | None:
        return next((ts for ts, text in self.texts.items() if key in text), None)

    def post(self, text: str, **extra: object) -> str:
        response = self.client.chat_postMessage(channel=self.id, text=text, **extra)
        ts = response["ts"]
        self.texts[ts] = text
        print(f"posted {ts}: {text.splitlines()[0]}")
        return ts

    def react(self, ts: str, name: str) -> None:
        self._tolerate(self.client.reactions_add, channel=self.id, timestamp=ts, name=name)

    def pin(self, ts: str) -> None:
        self._tolerate(self.client.pins_add, channel=self.id, timestamp=ts)

    def _tolerate(self, call: Callable[..., object], **params: object) -> None:
        """Makes the call, treating an already reacted or already pinned answer as done."""
        try:
            call(**params)
        except SlackApiError as error:
            if error.response.get("error") not in IGNORED_ERRORS:
                raise


def section(text: str) -> dict:
    return {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}


def styled(text: str, **style: bool) -> dict:
    return {"type": "text", "text": text, "style": style}


def bullets(style: str, indent: int, *items: str) -> dict:
    return {
        "type": "rich_text_list",
        "style": style,
        "indent": indent,
        "elements": [section(item) for item in items],
    }


def rich_text_blocks(channel_id: str) -> list[dict]:
    opening = [
        {"type": "text", "text": "seed: rich text with "},
        styled("bold", bold=True),
        {"type": "text", "text": ", "},
        styled("italic", italic=True),
        {"type": "text", "text": ", "},
        styled("struck out", strike=True),
        {"type": "text", "text": " and "},
        styled("inline code", code=True),
        {"type": "text", "text": ". A link: "},
        {"type": "link", "url": "https://example.com/docs", "text": "the docs"},
        {"type": "text", "text": ". Mentions: "},
        {"type": "user", "user_id": JIM},
        {"type": "text", "text": " "},
        {"type": "channel", "channel_id": channel_id},
        {"type": "text", "text": " "},
        {"type": "broadcast", "range": "here"},
        {"type": "text", "text": "\n"},
    ]
    return [
        {
            "type": "rich_text",
            "elements": [
                {"type": "rich_text_section", "elements": opening},
                bullets("bullet", 0, "first bullet", "second bullet"),
                bullets("bullet", 1, "nested bullet"),
                bullets("bullet", 0, "third bullet"),
                bullets("ordered", 0, "step one", "step two"),
                bullets("ordered", 1, "nested step"),
                {"type": "rich_text_quote", "elements": [{"type": "text", "text": "a quote"}]},
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": "def hello():\n    return 'world'"}],
                },
            ],
        }
    ]


def rich_text_fallback(channel_id: str) -> str:
    return (
        "seed: rich text with *bold*, _italic_, ~struck out~ and `inline code`. "
        f"A link: <https://example.com/docs|the docs>. Mentions: <@{JIM}> <#{channel_id}> "
        "<!here>\n- first bullet\n- second bullet\n  - nested bullet\n- third bullet\n"
        "1. step one\n2. step two\n  a. nested step\n> a quote\n"
        "```def hello():\n    return 'world'```"
    )


def tiny_pdf() -> bytes:
    content = b"BT /F1 18 Tf 20 40 Td (slarc seed) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", *args], check=True)


def make_files(directory: Path) -> dict[str, Path]:
    files = {
        "pdf": directory / "seed.pdf",
        "mp4": directory / "seed.mp4",
        "html": directory / "seed.html",
    }
    files["pdf"].write_bytes(tiny_pdf())
    files["html"].write_text(
        "<!doctype html>\n<html><head><title>slarc seed</title></head>"
        "<body><p>An uploaded HTML file.</p></body></html>\n"
    )
    source = "testsrc=duration=2:size=320x240:rate=15"
    ffmpeg("-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", str(files["mp4"]))
    for color in ("red", "green", "blue"):
        files[color] = directory / f"seed-{color}.png"
        ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=64x64", "-frames:v", "1", str(files[color]))
    return files


def upload(fixtures: Conversation, comment: str, paths: list[Path]) -> None:
    uploads = [{"file": str(path), "filename": path.name, "title": path.name} for path in paths]
    response = fixtures.client.files_upload_v2(
        channel=fixtures.id, initial_comment=comment, file_uploads=uploads
    )
    ids = ", ".join(f["id"] for f in response["files"])
    print(f"uploaded {ids}: {comment}")


def seed_messages(fixtures: Conversation) -> None:
    key = "seed: thread with a broadcast reply"
    if fixtures.find(key) is None:
        parent = fixtures.post(key)
        fixtures.post("seed: plain reply in the thread", thread_ts=parent)
        fixtures.post(
            "seed: broadcast reply, also sent to the channel",
            thread_ts=parent,
            reply_broadcast=True,
        )

    key = "seed: edited message"
    if fixtures.find(key) is None:
        ts = fixtures.post(f"{key}, original text")
        fixtures.client.chat_update(channel=fixtures.id, ts=ts, text=f"{key}, edited text")
        print(f"edited {ts}")

    key = "seed: reply under a deleted parent"
    if fixtures.find(key) is None:
        parent = fixtures.post("seed: parent deleted after it got a reply")
        fixtures.post(key, thread_ts=parent)
        fixtures.client.chat_delete(channel=fixtures.id, ts=parent)
        print(f"deleted {parent}")

    # A message deleted without replies leaves nothing behind, so repeating this is harmless.
    lone = fixtures.post("seed: message deleted without replies")
    fixtures.client.chat_delete(channel=fixtures.id, ts=lone)
    print(f"deleted {lone}")

    key = "seed: rich text"
    if fixtures.find(key) is None:
        fixtures.post(rich_text_fallback(fixtures.id), blocks=rich_text_blocks(fixtures.id))

    key = "seed: reactions"
    ts = fixtures.find(key) or fixtures.post(key)
    for name in ("wave", "wave::skin-tone-3", "thumbsup", "teamwork"):
        fixtures.react(ts, name)

    key = "seed: pinned message"
    ts = fixtures.find(key) or fixtures.post(key)
    fixtures.pin(ts)


def seed_files(fixtures: Conversation) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        files = make_files(Path(tmp))
        planned = [
            ("seed: a PDF file", [files["pdf"]]),
            ("seed: a short video", [files["mp4"]]),
            ("seed: three images", [files["red"], files["green"], files["blue"]]),
            ("seed: an HTML file", [files["html"]]),
        ]
        for comment, paths in planned:
            if fixtures.find(comment) is None:
                upload(fixtures, comment, paths)


def seed_topic_and_purpose(fixtures: Conversation) -> None:
    info = fixtures.client.conversations_info(channel=fixtures.id)["channel"]
    if info["topic"]["value"] != TOPIC:
        fixtures.client.conversations_setTopic(channel=fixtures.id, topic=TOPIC)
        print(f"set topic: {TOPIC}")
    if info["purpose"]["value"] != PURPOSE:
        fixtures.client.conversations_setPurpose(channel=fixtures.id, purpose=PURPOSE)
        print(f"set purpose: {PURPOSE}")


def seed_archived_channel(client: WebClient) -> None:
    listed = pages(client.conversations_list, "channels", types="public_channel")
    channel = next((c for c in listed if c["name"] == ARCHIVED_CHANNEL), None)
    if channel is not None and channel.get("is_archived"):
        return
    if channel is None:
        channel = client.conversations_create(name=ARCHIVED_CHANNEL, is_private=False)["channel"]
        print(f"created #{ARCHIVED_CHANNEL} {channel['id']}")
    archived = Conversation(client, channel["id"])
    for number in (1, 2, 3):
        text = f"seed: archived channel message {number}"
        if archived.find(text) is None:
            archived.post(text)
    client.conversations_archive(channel=archived.id)
    print(f"archived #{ARCHIVED_CHANNEL} {archived.id}")


def find_channel(client: WebClient, name: str) -> str:
    listed = pages(client.conversations_list, "channels", types="public_channel,private_channel")
    channel = next((c for c in listed if c["name"] == name), None)
    if channel is None:
        raise SystemExit(f"No channel named #{name} in the test workspace.")
    return channel["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, help="domain of the test workspace")
    parser.add_argument("--channel", required=True, help="name of the existing fixtures channel")
    args = parser.parse_args()

    sessions = DesktopSessionSource(Path.home(), sys.platform).sessions()
    session = next((s for s in sessions if s.domain == args.workspace), None)
    if session is None or session.team_id != TEAM_ID:
        print(f"Refusing: {args.workspace} is not the test workspace {TEAM_ID}.")
        return 1
    client = web_api(session).client
    if client.auth_test()["team_id"] != TEAM_ID:
        print(f"Refusing: auth.test does not report the test workspace {TEAM_ID}.")
        return 1

    fixtures = Conversation(client, find_channel(client, args.channel))
    if fixtures.find(MARKER) is not None:
        print(f"#{args.channel} already holds the marker message; nothing to do.")
        return 0
    try:
        seed_messages(fixtures)
        seed_files(fixtures)
        seed_topic_and_purpose(fixtures)
        seed_archived_channel(client)
        fixtures.post(MARKER)
    except SlackApiError as error:
        print(f"Slack refused a call: {error.response.get('error')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
