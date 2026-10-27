# ABOUTME: Tests for markdown export and for windows and --channel on export in both formats.
# ABOUTME: Pages land in an in-memory export writer; the recorded lake supplies the messages.
import os
from pathlib import Path

import pytest

from slarc.adapters.duckdb_index import DuckDbQueryIndex
from slarc.adapters.lake import extension_directory
from slarc.adapters.lake_store import DuckLakeArchiveStore
from slarc.domain.links import parse_selector
from slarc.domain.window import OPEN_WINDOW, Window
from slarc.formats import ExportFormat
from slarc.ports import SlarcError
from slarc.service.export import Exported, export
from tests.fakes import FakeArchiveLock, InMemoryBlobStore, InMemoryExportWriter
from tests.service.test_export import (
    ALL_NALEID,
    ALL_NALEID_PAGE,
    FIXTURES,
    FIXTURES_PAGE,
    JIM,
    MESSAGE_LINK,
    OUT,
    SYNTHETIC,
    TED,
    TEN_K_SNIPPET,
    THREAD_PAGE,
    append_line,
)

THREAD_PARENT = "1792427542.806979"
EARLY_REPLY = "1792427577.619569"
LATE_REPLIES = ["1792427679.271249", "1792427779.502989"]
AFTER_EARLY_REPLY = Window(since="1792427600.000000")
AFTER_ALL_NALEID = Window(since="1792437000.000000")


def exported(
    archive: Path,
    store: DuckLakeArchiveStore,
    blobs: InMemoryBlobStore,
    *selectors: str,
    fmt: ExportFormat = "markdown",
    window: Window = OPEN_WINDOW,
    whole_conversation: bool = False,
    single_file: bool = False,
) -> tuple[Exported, InMemoryExportWriter]:
    writer = InMemoryExportWriter()
    parsed = [parse_selector(text) for text in selectors]
    with DuckDbQueryIndex(
        archive, extension_directory(os.environ, Path.home()), FakeArchiveLock()
    ) as index:
        result = export(
            parsed,
            OUT,
            single_file,
            False,
            "America/Chicago",
            index,
            blobs,
            store,
            writer,
            fmt=fmt,
            window=window,
            whole_conversation=whole_conversation,
        )
    return result, writer


def link(ts: str, thread_ts: str | None = None) -> str:
    base = f"https://naleidworkspace.slack.com/archives/{ALL_NALEID}/p{ts.replace('.', '')}"
    return base + (f"?thread_ts={thread_ts}&cid={ALL_NALEID}" if thread_ts else "")


def line_with(page: str, text: str) -> str:
    return next(line for line in page.splitlines() if text in line)


def test_markdown_page_has_header_threads_files_and_reactions(archive, store, blobs) -> None:
    result, writer = exported(archive, store, blobs, "#all-naleid")
    assert result.path == OUT
    assert set(writer.files) == {OUT / f"{ALL_NALEID_PAGE}.md", OUT / "index.md"}
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.md")
    assert page.splitlines()[:2] == [
        "# #all-naleid (naleidworkspace)",
        "Window: the start to now, America/Chicago. 14 messages, 2 threads.",
    ]
    assert "\n## " in page
    # A real name that matches the display name shows once.
    assert line_with(page, link("1792422363.271059")).startswith("**Ted Naleid** 10:06 AM")
    parent = line_with(page, link(THREAD_PARENT))
    assert (parent.startswith("**") and " AM " in parent) or " PM " in parent
    reply = line_with(page, link(EARLY_REPLY, THREAD_PARENT))
    assert reply.startswith("> **")
    assert "[file: " in page
    assert ":wave: " in page
    assert "<" not in page.replace("<tag>", "")


def test_markdown_window_marks_an_older_parent_as_context(archive, store, blobs) -> None:
    result, writer = exported(archive, store, blobs, "#all-naleid", window=AFTER_EARLY_REPLY)
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.md")
    assert "(context)" in line_with(page, link(THREAD_PARENT))
    assert link(EARLY_REPLY, THREAD_PARENT) not in page
    for ts in LATE_REPLIES:
        assert "(context)" not in line_with(page, link(ts, THREAD_PARENT))
    # The two later replies and one join message fall in the window.
    assert page.splitlines()[1] == (
        "Window: 2026-10-19 11:33 to now, America/Chicago. 3 messages, 1 thread."
    )
    assert result.messages == 3


def test_window_drops_workspace_conversations_without_messages_in_it(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "naleidworkspace", window=AFTER_ALL_NALEID)
    names = {path.name for path in writer.files}
    assert f"{FIXTURES_PAGE}.md" in names
    assert f"{ALL_NALEID_PAGE}.md" not in names
    with pytest.raises(SlarcError, match=r"No messages of #all-naleid fall in the window\."):
        exported(archive, store, blobs, "#all-naleid", window=AFTER_ALL_NALEID)


def test_channel_flag_exports_the_conversation_and_marks_the_link(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, MESSAGE_LINK, whole_conversation=True)
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.md")
    assert page.startswith("# #all-naleid (naleidworkspace)\n")
    assert "(linked)" in line_with(page, link(EARLY_REPLY, THREAD_PARENT))
    assert page.count("(linked)") == 1


def test_thread_link_marks_the_linked_reply(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, MESSAGE_LINK)
    page = writer.text(OUT / f"{THREAD_PAGE}.md")
    assert page.startswith("# Thread in #all-naleid (naleidworkspace)\n")
    assert page.count("(linked)") == 1


def test_html_export_honors_the_window(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "#all-naleid", fmt="html", window=AFTER_EARLY_REPLY)
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.html")
    assert f'id="{ALL_NALEID}-1792410693.663869"' not in page
    assert f'id="{ALL_NALEID}-{LATE_REPLIES[0]}"' in page
    assert "(context)" in page


def test_markdown_single_file_holds_every_page(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "#all-naleid", "#slarc-fixtures", single_file=True)
    [(path, data)] = writer.files.items()
    assert path == Path("exported.md")
    text = data.decode()
    assert text.count("\n# ") + text.startswith("# ") == 2


def test_markdown_reply_on_a_later_day_gives_its_date(archive, store, blobs) -> None:
    parent = "1792437051.761119"
    reply = {"type": "message", "ts": SYNTHETIC, "thread_ts": parent, "user": JIM, "text": "later"}
    append_line(store, "message", reply, channel=FIXTURES, ts=SYNTHETIC)
    window = Window(since="1792490000.000000")
    _, writer = exported(archive, store, blobs, "#slarc-fixtures", window=window)
    page = writer.text(OUT / f"{FIXTURES_PAGE}.md")
    assert "## Monday, October 19, 2026" in page
    later = line_with(page, "p1792500000000100")
    assert later.startswith("> **Jim Naleid** (Jim) 2026-10-20 7:40 AM "), later
    assert "2026-10-19" not in line_with(page, f"p{parent.replace('.', '')}")


LINKED = f"https://naleidworkspace.slack.com/archives/{ALL_NALEID}/p1792427577619569"


def unfurl(text: str, blocks: list[dict] | None = None) -> dict:
    """A message unfurl as Slack sends it: no title, and a fallback repeating the raw text."""
    attachment = {
        "is_msg_unfurl": True,
        "from_url": LINKED,
        "author_id": JIM,
        "author_name": "Jim",
        "fallback": f"[October 19th, 2026 11:32 AM] jim: {text}",
        "text": text,
        "footer": "Thread in #all-naleid",
    }
    if blocks is not None:
        attachment["message_blocks"] = [{"message": {"blocks": blocks}}]
    return attachment


def test_markdown_message_unfurl_renders_once(archive, store, blobs) -> None:
    raw = f"first line for <@{TED}>\nsecond line"
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "first line for "},
                        {"type": "user", "user_id": TED},
                        {"type": "text", "text": "\nsecond line"},
                    ],
                }
            ],
        }
    ]
    parent = {
        "type": "message",
        "ts": SYNTHETIC,
        "user": TED,
        "text": "look",
        "attachments": [unfurl(raw, blocks)],
    }
    reply_ts = "1792500000.000200"
    reply = {
        "type": "message",
        "ts": reply_ts,
        "thread_ts": SYNTHETIC,
        "user": JIM,
        "text": "again",
        "attachments": [unfurl(raw)],
    }
    append_line(store, "message", parent, channel=FIXTURES, ts=SYNTHETIC)
    append_line(store, "message", reply, channel=FIXTURES, ts=reply_ts)
    _, writer = exported(
        archive, store, blobs, "#slarc-fixtures", window=Window(since="1792490000.000000")
    )
    page = writer.text(OUT / f"{FIXTURES_PAGE}.md")
    quoted = [
        f"> **Jim Naleid** (Jim) [linked message]({LINKED})",
        "> first line for @Ted Naleid",
        "> second line",
    ]
    assert "\n".join(quoted) in page
    assert "\n".join(f"> {line}" for line in quoted) in page
    assert "[preview" not in page
    assert "jim: first line" not in page


def test_markdown_preview_label_stays_on_one_line(archive, store, blobs) -> None:
    preview = {"fallback": "one\ntwo", "from_url": "https://example.com/a"}
    message = {
        "type": "message",
        "ts": SYNTHETIC,
        "user": TED,
        "text": "x",
        "attachments": [preview],
    }
    append_line(store, "message", message, channel=FIXTURES, ts=SYNTHETIC)
    _, writer = exported(
        archive, store, blobs, "#slarc-fixtures", window=Window(since="1792490000.000000")
    )
    page = writer.text(OUT / f"{FIXTURES_PAGE}.md")
    assert "[preview: one two](https://example.com/a)" in page


def test_markdown_directory_export_writes_an_index(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "#all-naleid", "#slarc-fixtures")
    assert {path.name for path in writer.files} == {
        "index.md",
        f"{ALL_NALEID_PAGE}.md",
        f"{FIXTURES_PAGE}.md",
    }
    index = writer.text(OUT / "index.md").splitlines()
    assert index[:2] == [
        "# slarc export",
        "Window: the start to now, America/Chicago. 2 pages.",
    ]
    assert index[3] == (
        f"- [{ALL_NALEID_PAGE}.md]({ALL_NALEID_PAGE}.md): #all-naleid (naleidworkspace), "
        "14 messages, 2 threads."
    )
    assert index[4].startswith(
        f"- [{FIXTURES_PAGE}.md]({FIXTURES_PAGE}.md): #slarc-fixtures (naleidworkspace), "
    )
    assert len(index) == 5


def fenced_after(page: str, marker: str) -> list[str]:
    """The lines of the fenced block that starts on the line after the one holding marker,
    fences included."""
    lines = page.splitlines()
    start = next(n for n, line in enumerate(lines) if marker in line) + 1
    fence = lines[start].rstrip("abcdefghijklmnopqrstuvwxyz")
    end = lines.index(fence, start + 1)
    return lines[start : end + 1]


def test_markdown_snippet_text_follows_its_file_line(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "#all-naleid")
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.md")
    block = fenced_after(page, "[file: test-snippet-2.txt")
    assert block[0] == "```text"
    assert block[-1] == "```"
    assert len(block) > 600
    assert "[cut at" not in page.split("[file: test-snippet-2.txt")[1].split("\n\n")[0]


def test_markdown_snippet_over_the_limit_is_cut(archive, store, blobs) -> None:
    _, writer = exported(archive, store, blobs, "#all-naleid")
    page = writer.text(OUT / f"{ALL_NALEID_PAGE}.md")
    block = fenced_after(page, "[file: 10k-snippet.txt")
    whole = blobs.read(TEN_K_SNIPPET).decode()
    shown = "\n".join(block[1:-1])
    assert whole.startswith(shown)
    assert 199 * 1024 < len(shown.encode()) <= 200 * 1024
    after = page.split(f"{shown}\n```\n", 1)[1].splitlines()[0]
    assert after == "[cut at 200 KB; the rest: slarc file F0CA7TKM285]"


def test_markdown_snippet_fence_outlasts_backticks_in_its_text(archive, store, blobs) -> None:
    text = "before\n```\ncode\n```\nafter\n"
    blob = blobs.write([text.encode()])[0]
    file = {"id": "F0FENCE", "name": "notes.md", "mode": "snippet", "filetype": "markdown"}
    append_line(store, "file", {**file, "size": len(text)}, id="F0FENCE", blob=blob)
    message = {"type": "message", "ts": SYNTHETIC, "user": TED, "text": "n", "files": [file]}
    append_line(store, "message", message, channel=FIXTURES, ts=SYNTHETIC)
    window = Window(since="1792490000.000000")
    _, writer = exported(archive, store, blobs, "#slarc-fixtures", window=window)
    page = writer.text(OUT / f"{FIXTURES_PAGE}.md")
    assert "\n````markdown\nbefore\n```\ncode\n```\nafter\n````\n" in page


def test_markdown_snippet_without_a_blob_shows_its_preview(archive, store, blobs) -> None:
    file = {"id": "F0PREVIEW", "name": "p.txt", "mode": "snippet", "filetype": "text"}
    append_line(store, "file", {**file, "preview": "first lines"}, id="F0PREVIEW")
    message = {"type": "message", "ts": SYNTHETIC, "user": TED, "text": "n", "files": [file]}
    append_line(store, "message", message, channel=FIXTURES, ts=SYNTHETIC)
    window = Window(since="1792490000.000000")
    _, writer = exported(archive, store, blobs, "#slarc-fixtures", window=window)
    page = writer.text(OUT / f"{FIXTURES_PAGE}.md")
    assert "```text\nfirst lines\n```\nOnly the start of this snippet was archived.\n" in page
