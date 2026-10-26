# ABOUTME: page_markdown renders an export page as markdown for reading as text, by people or
# ABOUTME: agents: a header with the window and counts, then messages with threads quoted.
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING

from slarc.domain.links import MessageTs, is_web_url, message_link
from slarc.domain.render import dict_list, text_field
from slarc.domain.render.markdown import mrkdwn_markdown, render_message_markdown

if TYPE_CHECKING:
    from slarc.domain.directory import DirectoryView
    from slarc.service.export import Entry, FileView, MessageView, Page

BOUND = "%Y-%m-%d %H:%M"


def page_markdown(
    page: Page, domain: str, view: DirectoryView, zone: tzinfo, tz: str | None
) -> str:
    """The page as markdown. Times are in zone, which tz names, or else the zone's own name."""
    renderer = _Renderer(page, domain, view)
    zone_name = tz or str(zone)
    start = _bound(page.window.since, zone) or "the start"
    end = _bound(page.window.until, zone) or "now"
    threads = _thread_count(page)
    lines = [
        f"# {page.title} ({domain})",
        f"Window: {start} to {end}, {zone_name}. "
        f"{_plural(page.count, 'message')}, {_plural(threads, 'thread')}.",
    ]
    for entry in page.entries:
        lines += ["", *renderer.entry(entry)]
    return "\n".join(lines) + "\n"


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
        return f"{message.author} {body}" if message.author else body

    def message(self, message: MessageView) -> list[str]:
        head = [f"**{message.author or 'unknown'}**"]
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
        lines += [_file_line(file) for file in message.files]
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
        """A link preview or bot attachment: its title as a link, then its text quoted."""
        lines = []
        for attachment in dict_list(source.get("attachments")):
            title = text_field(attachment.get("title")) or text_field(attachment.get("fallback"))
            url = attachment.get("title_link") or attachment.get("from_url")
            service = text_field(attachment.get("service_name"))
            label = f"{service}: {title}" if service and title else service or title
            if label:
                lines.append(
                    f"[preview: {label}]({url})" if is_web_url(url) else f"[preview: {label}]"
                )
            text = text_field(attachment.get("text"))
            if text:
                lines += [f"> {line}" for line in mrkdwn_markdown(text, self.view).split("\n")]
        return lines


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
