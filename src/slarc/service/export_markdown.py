# ABOUTME: page_markdown renders an export page as markdown for reading as text, by people or
# ABOUTME: agents: a header with the window and counts, then messages with threads quoted.
import re
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING

from slarc.domain.links import MessageTs, is_web_url, message_link
from slarc.domain.render import dict_list, text_field
from slarc.domain.render.markdown import mrkdwn_markdown, render_message_markdown
from slarc.ports import SNIPPET_LIMIT

if TYPE_CHECKING:
    from slarc.domain.directory import DirectoryView
    from slarc.service.export import Entry, FileView, MessageView, Page

BOUND = "%Y-%m-%d %H:%M"
BACKTICKS = re.compile("`+")


def page_markdown(
    page: Page, domain: str, view: DirectoryView, zone: tzinfo, tz: str | None
) -> str:
    """The page as markdown. Times are in zone, which tz names, or else the zone's own name."""
    renderer = _Renderer(page, domain, view)
    lines = [
        f"# {page.title} ({domain})",
        f"Window: {_window_text(page, zone, tz)}. {_counts(page)}.",
    ]
    for entry in page.entries:
        lines += ["", *renderer.entry(entry)]
    return "\n".join(lines) + "\n"


def index_markdown(pages: list[Page], domains: dict[str, str], zone: tzinfo, tz: str | None) -> str:
    """A directory export's index.md: the window, then a line per page with its file,
    conversation and counts. Every page of an export shares one window."""
    lines = [
        "# slarc export",
        f"Window: {_window_text(pages[0], zone, tz)}. {_plural(len(pages), 'page')}.",
        "",
    ]
    for page in pages:
        name = f"{page.stem}.md"
        domain = domains.get(page.team, page.team)
        lines.append(f"- [{name}]({name}): {page.title} ({domain}), {_counts(page)}.")
    return "\n".join(lines) + "\n"


def _window_text(page: Page, zone: tzinfo, tz: str | None) -> str:
    start = _bound(page.window.since, zone) or "the start"
    end = _bound(page.window.until, zone) or "now"
    return f"{start} to {end}, {tz or str(zone)}"


def _counts(page: Page) -> str:
    return f"{_plural(page.count, 'message')}, {_plural(_thread_count(page), 'thread')}"


class _Renderer:
    def __init__(self, page: Page, domain: str, view: DirectoryView) -> None:
        self.page = page
        self.domain = domain
        self.view = view
        self.day = ""

    def entry(self, entry: Entry) -> list[str]:
        if entry.kind == "day":
            self.day = entry.moment.day
            return [f"## {entry.moment.day_label}"]
        if entry.kind == "system":
            texts = [self.system_text(message) for message in entry.messages]
            return [f"_{'; '.join(text for text in texts if text)}_"]
        [message] = entry.messages
        lines = self.message(message)
        if message.thread is not None:
            for reply in message.thread.replies:
                lines += [">", *(f"> {line}" if line else ">" for line in self.message(reply))]
        return lines

    def system_text(self, message: MessageView) -> str:
        body = render_message_markdown(message.source, self.view).replace("\n", " ")
        name = message.author_real_name
        return f"{name} {body}" if name else body

    def message(self, message: MessageView) -> list[str]:
        """The message's lines. The head names the author by real name, with the display name
        after it when the two differ."""
        name = message.author_real_name or "unknown"
        head = [f"**{name}**"]
        if message.author and message.author != name:
            head.append(f"({message.author})")
        if message.app:
            head.append(f"via {message.app}")
        # A reply posted on a later day than its heading's gives its date.
        if message.time.day != self.day:
            head.append(message.time.day)
        head.append(message.time.clock)
        if message.edited:
            head.append("(edited)")
        if message.deleted:
            head.append(f"(deleted {message.deleted.full})")
        if message.context:
            head.append("(context)")
        if message.linked:
            head.append("(linked)")
        if message.broadcast == "to_channel":
            head.append("(also sent to the channel)")
        elif message.broadcast == "from_thread":
            head.append("(a thread reply, also sent to the channel)")
        head.append(self.link(message))
        lines = [" ".join(head)]
        body = render_message_markdown(message.source, self.view)
        if body:
            lines += body.split("\n")
        lines += self.attachments(message.source)
        for file in message.files:
            lines += [_file_line(file), *_snippet_lines(file)]
        reactions = _reactions(message.source)
        if reactions:
            lines.append(reactions)
        return lines

    def link(self, message: MessageView) -> str:
        thread_ts = text_field(message.source.get("thread_ts"))
        reply = thread_ts and thread_ts != message.ts
        return message_link(
            self.domain, self.page.channel, message.ts, thread_ts if reply else None
        )

    def attachments(self, source: dict) -> list[str]:
        """Each attachment's lines: a linked message quoted, or a link preview or bot
        attachment's title as a link, then its text quoted."""
        lines = []
        for attachment in dict_list(source.get("attachments")):
            if attachment.get("is_msg_unfurl"):
                lines += self.linked_message(attachment)
                continue
            title = text_field(attachment.get("title")) or text_field(attachment.get("fallback"))
            url = attachment.get("title_link") or attachment.get("from_url")
            service = text_field(attachment.get("service_name"))
            label = " ".join(
                (f"{service}: {title}" if service and title else service or title).split()
            )
            if label:
                lines.append(
                    f"[preview: {label}]({url})" if is_web_url(url) else f"[preview: {label}]"
                )
            text = text_field(attachment.get("text"))
            if text:
                lines += [f"> {line}" for line in mrkdwn_markdown(text, self.view).split("\n")]
        return lines

    def linked_message(self, unfurl: dict) -> list[str]:
        """A Slack message a link points to, quoted: its author and link, then its blocks, or
        else its text, with mentions resolved."""
        author_id = text_field(unfurl.get("author_id"))
        known = self.view.users.get(author_id) is not None
        author = self.view.author_display({"user": author_id}) if known else ""
        name = self.view.author_real_name({"user": author_id}) if known else ""
        name = name or text_field(unfurl.get("author_name")) or "unknown"
        head = f"**{name}** ({author})" if author and author != name else f"**{name}**"
        url = unfurl.get("from_url")
        head += f" [linked message]({url})" if is_web_url(url) else " (linked message)"
        blocks = [
            block
            for entry in dict_list(unfurl.get("message_blocks"))
            for block in dict_list((entry.get("message") or {}).get("blocks"))
        ]
        if blocks:
            body = render_message_markdown({"blocks": blocks}, self.view)
        else:
            body = mrkdwn_markdown(text_field(unfurl.get("text")), self.view)
        lines = [head, *body.split("\n")] if body else [head]
        return [f"> {line}" if line else ">" for line in lines]


def _file_line(file: FileView) -> str:
    match file.kind:
        case "tombstone":
            return f"[file deleted: {file.name}]"
        case "hidden_by_limit":
            return f"[file hidden by Slack's plan limit: {file.name}]"
        case "external":
            return f"[file: {file.name}]({file.link})" if file.link else f"[file: {file.name}]"
    details = [file.name]
    if file.size:
        details.append(file.size)
    if file.kind == "too_large":
        details.append("too large to archive")
    elif file.kind == "missing":
        details.append("not in the archive")
    label = f"[file: {', '.join(details)}]"
    return f"{label}({file.permalink})" if file.permalink else label


def _snippet_lines(file: FileView) -> list[str]:
    """A snippet's text as a fenced block longer than any run of backticks in it, then a note
    when the text is cut or only its preview was archived."""
    if file.kind != "snippet":
        return []
    lines = []
    if file.text:
        longest = max((len(run) for run in BACKTICKS.findall(file.text)), default=0)
        fence = "`" * max(3, longest + 1)
        lines = [fence + file.language, *file.text.removesuffix("\n").split("\n"), fence]
    if file.cut:
        lines.append(f"[cut at {SNIPPET_LIMIT // 1024} KB; the rest: slarc file {file.file_id}]")
    if file.note:
        lines.append(file.note)
    return lines


def _reactions(source: dict) -> str:
    parts = []
    for reaction in dict_list(source.get("reactions")):
        name = text_field(reaction.get("name"))
        count = reaction.get("count")
        if name:
            parts.append(f":{name}: {count if isinstance(count, int) else 1}")
    return "  ".join(parts)


def _thread_count(page: Page) -> int:
    """Threads with a reply shown on the page."""
    parents = {
        row["thread_ts"]
        for row in page.rows
        if not row.get("context") and row["thread_ts"] and row["thread_ts"] != row["ts"]
    }
    return len(parents)


def _bound(ts: str | None, zone: tzinfo) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(MessageTs(ts)), zone).strftime(BOUND)


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"
