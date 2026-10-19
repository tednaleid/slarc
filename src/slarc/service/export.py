# ABOUTME: Export use case: renders the conversations and threads selectors name as static HTML.
# ABOUTME: Writes index.html, one page each and assets/<sha256>.<ext>, or one file with data URIs.
import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from functools import cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, PackageLoader, StrictUndefined
from markupsafe import Markup
from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

from slarc.domain.directory import DirectoryView, directory_image_url
from slarc.domain.emoji import resolve_emoji
from slarc.domain.links import ConversationSel, MessageSel, Selector, is_web_url
from slarc.domain.render.blocks import render_message_html
from slarc.domain.render.mrkdwn import emoji_html, render_mrkdwn_html
from slarc.domain.tracking import DownloadState
from slarc.ports import (
    ArchiveStore,
    BlobStore,
    DirectoryState,
    ExportWriter,
    QueryIndex,
    SlarcError,
)
from slarc.service.listing import workspace_domains
from slarc.service.selectors import archived_conversation, only_workspace

ISO_SECONDS = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_TITLE = "slarc export"
CONTENT_TYPE_EXTENSIONS = {
    "image/png": "png",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "application/pdf": "pdf",
    "text/plain": "txt",
}
EXTENSION_CONTENT_TYPES = {
    ext: content_type for content_type, ext in CONTENT_TYPE_EXTENSIONS.items()
}
# Image suffixes an image URL may give its asset; anything else without a known type is bin.
IMAGE_EXTENSIONS = frozenset({"png", "gif", "jpg", "jpeg", "webp", "bmp", "ico", "avif"})
# Extensions a browser would run as a page or script when an asset is opened from the export.
ACTIVE_EXTENSIONS = frozenset({"htm", "html", "xhtml", "js", "mjs", "svg"})
EXTENSION = re.compile(r"[a-z0-9]{1,8}")
COLOR = re.compile(r"#?([0-9a-fA-F]{6})")
NAMED_COLORS = {"good": "#2eb886", "warning": "#daa038", "danger": "#a30200"}
MEDIA_TYPE = re.compile(r"[a-z]+/[a-z0-9.+-]+")
UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ICON_KEYS = ("image_72", "image_48", "image_36")
# System messages whose text leaves out who acted, so the author is shown before it.
ACTOR_SUBTYPES = frozenset(
    {"channel_topic", "channel_purpose", "channel_name", "channel_archive", "pinned_item"}
)


@dataclass(frozen=True)
class Moment:
    """A time shown in the export's zone; JS reformats it in the viewer's zone without --tz."""

    iso: str
    clock: str
    full: str
    day: str
    day_label: str


@dataclass(frozen=True)
class Picture:
    """A chrome image that repeats, such as an avatar: an src, or in a single-file export a
    blob-<sha256> class whose one CSS rule holds the image's data: URI."""

    src: str | None
    blob_class: str | None

    def html(self, css_class: str, label: str) -> Markup:
        if self.blob_class is not None:
            return Markup(
                '<span class="{} {}" role="img" aria-label="{}" title="{}"></span>'
            ).format(css_class, self.blob_class, label, label)
        return Markup('<img class="{}" src="{}" alt="{}" title="{}">').format(
            css_class, self.src, label, label
        )


@dataclass(frozen=True)
class Avatar:
    picture: Picture | None
    name: str


@dataclass(frozen=True)
class Reaction:
    emoji: list[Markup]
    count: int
    title: str


@dataclass(frozen=True)
class Attachment:
    color: str | None
    icon: Picture | None
    service: str
    title: str
    link: str | None
    text: Markup
    image: str | None
    thumb: str | None
    fallback: str


type FileKind = Literal[
    "image",
    "video",
    "audio",
    "snippet",
    "download",
    "tombstone",
    "hidden_by_limit",
    "too_large",
    "external",
    "missing",
]


@dataclass(frozen=True)
class FileView:
    kind: FileKind
    name: str
    src: str | None = None
    mimetype: str = ""
    size: str = ""
    code: Markup | None = None
    note: str = ""
    link: str | None = None


@dataclass(frozen=True)
class Thread:
    replies: list[MessageView]
    participants: list[Avatar]
    latest: Moment | None
    open: bool


@dataclass(frozen=True)
class MessageView:
    anchor: str
    ts: str
    author: str
    avatar: Picture | None
    app: str | None
    time: Moment
    author_time: str
    edited: Moment | None
    body: Markup
    attachments: list[Attachment]
    files: list[FileView]
    reactions: list[Reaction]
    thread: Thread | None
    system: bool
    deleted: Moment | None
    broadcast: Literal["to_channel", "from_thread"] | None
    parent_anchor: str | None


@dataclass(frozen=True)
class Entry:
    """One item of a page's message list: a day divider, a run of system messages or a message."""

    kind: Literal["day", "system", "message"]
    moment: Moment
    messages: list[MessageView] = field(default_factory=list)


@dataclass(frozen=True)
class Page:
    filename: str
    name: str
    thread_ts: str | None
    title: str
    count: int
    entries: list[Entry]

    @property
    def section_id(self) -> str:
        return "page-" + self.filename.removesuffix(".html")


def export(
    selectors: list[Selector],
    out: Path | None,
    single_file: bool,
    include_deleted: bool,
    tz: str | None,
    index: QueryIndex,
    blobs: BlobStore,
    archive: ArchiveStore,
    writer: ExportWriter,
) -> Path:
    """Writes what selectors name and returns where: the directory, or the file with single_file.

    Without out, the export goes to slarc-export-<first conversation's name> under the current
    directory.
    """
    zone = _zone(tz)
    domains = {domain: team for team, domain in workspace_domains(index).items()}
    used: dict[str, str] = {}
    styles: dict[str, str] = {}
    workspaces: dict[str, _Workspace] = {}
    pages: dict[str, Page] = {}
    for selector in selectors:
        team = _team(selector, domains, archive)
        if team not in workspaces:
            workspaces[team] = _Workspace.load(
                team, archive, blobs, single_file, used, styles, zone
            )
        workspace = workspaces[team]
        page = _page(selector, workspace, index, include_deleted)
        pages.setdefault(page.filename, page)
    first = next(iter(pages.values()))
    stem = first.name + (f"-{first.thread_ts}" if first.thread_ts else "")
    default = "slarc-export-" + (UNSAFE_FILENAME.sub("-", stem).strip("-.") or "export")
    css = _static("viewer.css") + HtmlFormatter().get_style_defs(".highlight")
    if single_file:
        css += "".join(
            f'\n.blob-{blob}{{background-image:url("{uri}")}}' for blob, uri in styles.items()
        )
    js = _static("viewer.js")
    common = {"tz": tz, "inline": single_file, "css": Markup(css), "js": Markup(js)}
    conversation = _templates().get_template("conversation.html.j2")
    if single_file:
        target = _html_path(out or Path(default))
        title = next(iter(pages.values())).title if len(pages) == 1 else DEFAULT_TITLE
        html = conversation.render(
            title=title, pages=list(pages.values()), nav=len(pages) > 1, **common
        )
        writer.write(target, html.encode())
        return target
    target = out or Path(default)
    index_html = (
        _templates()
        .get_template("index.html.j2")
        .render(title=DEFAULT_TITLE, pages=list(pages.values()), **common)
    )
    writer.write(target / "index.html", index_html.encode())
    for page in pages.values():
        html = conversation.render(title=page.title, pages=[page], nav=False, **common)
        writer.write(target / page.filename, html.encode())
    writer.write(target / "viewer.css", css.encode())
    writer.write(target / "viewer.js", js.encode())
    for name, sha256 in sorted(used.items()):
        writer.write(target / "assets" / name, blobs.read(sha256))
    return target


@cache
def _templates() -> Environment:
    return Environment(
        loader=PackageLoader("slarc", "export/templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _static(name: str) -> str:
    return files("slarc").joinpath(f"export/static/{name}").read_text(encoding="utf-8")


def _zone(tz: str | None) -> tzinfo:
    if tz is None:
        return UTC
    try:
        return ZoneInfo(tz)
    except ZoneInfoNotFoundError, ValueError:
        raise SlarcError(f"Unknown time zone '{tz}'. Use a name such as America/Chicago.") from None


def _html_path(path: Path) -> Path:
    return path if path.suffix.lower() in (".html", ".htm") else path.with_name(path.name + ".html")


def _team(selector: Selector, domains: dict[str, str], archive: ArchiveStore) -> str:
    if selector.domain is None:
        return only_workspace(archive.tracked_items())
    team = domains.get(selector.domain)
    if team is None:
        raise SlarcError(f"This archive holds no workspace {selector.domain}.")
    return team


def _channel(selector: Selector, workspace: _Workspace) -> str:
    if isinstance(selector, MessageSel):
        return selector.channel
    return archived_conversation(selector, workspace.team, workspace.archive)


def _page(
    selector: Selector, workspace: _Workspace, index: QueryIndex, include_deleted: bool
) -> Page:
    team = workspace.team
    channel = _channel(selector, workspace)
    label = workspace.view.conversation_label(channel)
    if isinstance(selector, ConversationSel):
        rows = index.messages(team, channel, None, None, include_deleted)
        filename, title, thread_ts = f"{channel}.html", label, None
    else:
        thread_ts = selector.thread_ts or _thread_of(index, team, channel, selector.ts)
        rows = index.thread(team, channel, thread_ts, include_deleted)
        filename, title = f"{channel}-{thread_ts}.html", f"Thread in {label}"
    rows = [row for row in rows if row["object"] is not None]
    if not rows:
        raise SlarcError(f"The archive holds no messages for {title}. Pull it first.")
    messages = _top_level(rows, channel, workspace, thread_ts is not None)
    return Page(
        filename=filename,
        name=workspace.view.conversation_name(channel),
        thread_ts=thread_ts,
        title=title,
        count=len(rows),
        entries=_entries(messages),
    )


def _thread_of(index: QueryIndex, team: str, channel: str, ts: str) -> str:
    """The thread a message link names: its message's thread, or the message itself."""
    found = [row for row in index.thread(team, channel, ts, True) if row["ts"] == ts]
    return (found[0]["thread_ts"] if found else None) or ts


def _top_level(
    rows: list[dict], channel: str, workspace: _Workspace, thread_page: bool
) -> list[MessageView]:
    """The messages shown in the page's flow, each with its thread's replies under it.

    A reply whose parent is not exported is shown in the flow; a broadcast reply is shown in
    its thread and, on a conversation page, in the flow as well.
    """
    present = {row["ts"] for row in rows}
    replies: dict[str, list[dict]] = {}
    flow = []
    for row in rows:
        parent = row["thread_ts"]
        is_reply = parent is not None and parent != row["ts"] and parent in present
        if is_reply:
            replies.setdefault(parent, []).append(row)
        if not is_reply or (row["is_broadcast"] and not thread_page):
            flow.append(row)
    views = []
    for row in flow:
        thread_rows = replies.get(row["ts"]) if row["thread_ts"] == row["ts"] else None
        thread = None
        if thread_rows:
            thread = workspace.thread(thread_rows, channel, thread_page)
        in_flow_broadcast = row["is_broadcast"] and row["thread_ts"] in present
        views.append(
            workspace.message(row, channel, thread, "from_thread" if in_flow_broadcast else None)
        )
    return views


def _entries(messages: list[MessageView]) -> list[Entry]:
    entries: list[Entry] = []
    day = None
    for message in messages:
        if message.time.day != day:
            entries.append(Entry("day", message.time))
            day = message.time.day
        if message.system and message.thread is None:
            if entries[-1].kind == "system":
                entries[-1].messages.append(message)
            else:
                entries.append(Entry("system", message.time, [message]))
        else:
            entries.append(Entry("message", message.time, [message]))
    return entries


@dataclass
class _Assets:
    """AssetResolver over one workspace's directory lines. A src is a copied blob's path under
    assets/, recorded in used, or a data: URI when the export is one file. A picture in a
    single-file export is a class whose data: URI is recorded once in styles."""

    view: DirectoryView
    blobs: BlobStore
    inline: bool
    used: dict[str, str]
    styles: dict[str, str]
    images: dict[str, DirectoryState]
    image_types: dict[str, str]
    emoji: dict[str, DirectoryState]
    file_states: dict[str, DirectoryState]
    file_objects: dict[str, dict]
    data_uris: dict[str, str] = field(default_factory=dict)

    def emoji_src(self, name: str) -> str | None:
        url = self.view.emoji.get(name) or ""
        content_type = self.image_types.get(url)
        return self._src(self.emoji.get(name), _image_extension(url, content_type), content_type)

    def image_src(self, url: str) -> str | None:
        content_type = self.image_types.get(url)
        return self._src(self.images.get(url), _image_extension(url, content_type), content_type)

    def picture(self, url: str) -> Picture | None:
        if not self.inline:
            src = self.image_src(url)
            return Picture(src, None) if src else None
        state = self.images.get(url)
        blob = state.blob if state else None
        if not blob or not self.blobs.exists(blob):
            return None
        content_type = self.image_types.get(url)
        self.styles[blob] = self._data_uri(blob, _image_extension(url, content_type), content_type)
        return Picture(None, f"blob-{blob}")

    def file_src(self, file_id: str) -> str | None:
        obj = self.file_objects.get(file_id) or {}
        mimetype = _text(obj.get("mimetype"))
        extension = _extension(_text(obj.get("name")), mimetype)
        return self._src(self.file_states.get(file_id), extension, mimetype)

    def file_blob(self, file_id: str) -> str | None:
        state = self.file_states.get(file_id)
        return state.blob if state and state.blob and self.blobs.exists(state.blob) else None

    def _src(
        self, state: DirectoryState | None, extension: str, content_type: str | None
    ) -> str | None:
        blob = state.blob if state else None
        if not blob or not self.blobs.exists(blob):
            return None
        if self.inline:
            return self._data_uri(blob, extension, content_type)
        name = f"{blob}.{extension}"
        self.used[name] = blob
        return f"assets/{name}"

    def _data_uri(self, blob: str, extension: str, content_type: str | None) -> str:
        if blob not in self.data_uris:
            media = content_type if MEDIA_TYPE.fullmatch(content_type or "") else None
            media = media or EXTENSION_CONTENT_TYPES.get(extension) or "application/octet-stream"
            encoded = base64.b64encode(self.blobs.read(blob)).decode()
            self.data_uris[blob] = f"data:{media};base64,{encoded}"
        return self.data_uris[blob]


@dataclass
class _Workspace:
    """Renders one workspace's messages with its directory, assets and download states."""

    team: str
    archive: ArchiveStore
    blobs: BlobStore
    view: DirectoryView
    assets: _Assets
    downloads: dict[tuple[str, str], DownloadState]
    zone: tzinfo

    @classmethod
    def load(
        cls,
        team: str,
        archive: ArchiveStore,
        blobs: BlobStore,
        inline: bool,
        used: dict[str, str],
        styles: dict[str, str],
        zone: tzinfo,
    ) -> _Workspace:
        emoji = archive.directory_objects(team, "emoji")
        view = DirectoryView(
            users=archive.directory_objects(team, "user"),
            conversations=archive.directory_objects(team, "conversation"),
            usergroups=archive.directory_objects(team, "usergroup"),
            emoji={name: _text(obj.get("url")) for name, obj in emoji.items()},
            bots=archive.directory_objects(team, "bot"),
        )
        image_objects = archive.directory_objects(team, "image")
        assets = _Assets(
            view=view,
            blobs=blobs,
            inline=inline,
            used=used,
            styles=styles,
            images=archive.directory_state(team, "image"),
            image_types={url: _text(obj.get("content_type")) for url, obj in image_objects.items()},
            emoji=archive.directory_state(team, "emoji"),
            file_states=archive.directory_state(team, "file"),
            file_objects=archive.directory_objects(team, "file"),
        )
        return cls(team, archive, blobs, view, assets, archive.downloads(), zone)

    def thread(self, rows: list[dict], channel: str, thread_page: bool) -> Thread:
        # On a conversation page a broadcast reply's anchor belongs to its copy in the flow.
        replies = [
            self.message(row, channel, None, "to_channel", "" if thread_page else "-thread")
            if row["is_broadcast"]
            else self.message(row, channel, None, None)
            for row in rows
        ]
        authors = list(dict.fromkeys(row["user_id"] for row in rows if row["user_id"]))
        participants = [
            Avatar(self.avatar_of(user_id), self.view.user_display(user_id)) for user_id in authors
        ]
        return Thread(replies, participants, replies[-1].time if replies else None, thread_page)

    def message(
        self,
        row: dict,
        channel: str,
        thread: Thread | None,
        broadcast: Literal["to_channel", "from_thread"] | None,
        anchor_suffix: str = "",
    ) -> MessageView:
        message = row["object"]
        system = row["author_kind"] == "system"
        edited = (message.get("edited") or {}).get("ts")
        deleted_at = row["deleted_at"]
        parent = row["thread_ts"]
        named = not system or message.get("subtype") in ACTOR_SUBTYPES
        return MessageView(
            anchor=f"{channel}-{row['ts']}{anchor_suffix}",
            ts=row["ts"],
            author=self.view.author_display(message) if named else "",
            avatar=None if system else self.author_avatar(message),
            app=self.app_name(message),
            time=self.moment(_seconds(row["ts"])),
            author_time=self.author_time(message, _seconds(row["ts"])) if named else "",
            edited=self.moment(_seconds(edited)) if edited and not system else None,
            body=render_message_html(message, self.view, self.assets),
            attachments=self.attachments(message),
            files=[self.file(entry) for entry in _dicts(message.get("files"))],
            reactions=self.reactions(message),
            thread=thread,
            system=system,
            deleted=self.moment(_parse_iso(deleted_at)) if deleted_at else None,
            broadcast=broadcast,
            parent_anchor=f"{channel}-{parent}" if broadcast == "from_thread" else None,
        )

    def moment(self, seconds: int) -> Moment:
        at = datetime.fromtimestamp(seconds, UTC)
        local = at.astimezone(self.zone)
        day_label = f"{local:%A, %B} {local.day}, {local.year}"
        return Moment(
            iso=at.strftime(ISO_SECONDS),
            clock=_clock(local),
            full=f"{day_label} at {_clock(local)} {local.tzname()}",
            day=local.strftime("%Y-%m-%d"),
            day_label=day_label,
        )

    def author_time(self, message: dict, seconds: int) -> str:
        """The message's time where its author is, from the author's time zone, or ""."""
        user = self.view.users.get(_text(message.get("user"))) or {}
        tz = _text(user.get("tz"))
        if not tz:
            return ""
        try:
            local = datetime.fromtimestamp(seconds, UTC).astimezone(ZoneInfo(tz))
        except ZoneInfoNotFoundError, ValueError:
            return ""
        return f"{_clock(local)} {local.tzname()} for the author"

    def avatar_of(self, user_id: str) -> Picture | None:
        url = directory_image_url("user", self.view.users.get(user_id) or {})
        return self.assets.picture(url) if url else None

    def author_avatar(self, message: dict) -> Picture | None:
        """The user's avatar, else the first bot icon the archive holds a copy of."""
        user_id = _text(message.get("user"))
        if user_id in self.view.users:
            return self.avatar_of(user_id)
        bot = self.view.bots.get(_text(message.get("bot_id"))) or {}
        for icons in (
            message.get("icons"),
            (message.get("bot_profile") or {}).get("icons"),
            bot.get("icons"),
        ):
            if not isinstance(icons, dict):
                continue
            for key in ICON_KEYS:
                url = icons.get(key)
                picture = self.assets.picture(url) if is_web_url(url) else None
                if picture is not None:
                    return picture
        return None

    def app_name(self, message: dict) -> str | None:
        """The app a person posted through, such as giphy; a bot's own posts name no app."""
        name = (message.get("bot_profile") or {}).get("name")
        return name if message.get("user") and isinstance(name, str) and name else None

    def reactions(self, message: dict) -> list[Reaction]:
        """Reactions grouped by base name, the part before ::, with skin tones side by side."""
        groups: dict[str, list[dict]] = {}
        for reaction in _dicts(message.get("reactions")):
            name = reaction.get("name")
            if isinstance(name, str) and name:
                groups.setdefault(name.split("::")[0], []).append(reaction)
        shown = []
        for variants in groups.values():
            names = [variant["name"] for variant in variants]
            user_ids = [u for v in variants for u in v.get("users") or [] if isinstance(u, str)]
            people = list(dict.fromkeys(self.view.user_display(u) for u in user_ids))
            count = sum(v["count"] if isinstance(v.get("count"), int) else 0 for v in variants)
            shown.append(
                Reaction(
                    emoji=[
                        emoji_html(name, resolve_emoji(name, self.view, self.assets.emoji_src))
                        for name in names
                    ],
                    count=count or len(user_ids),
                    title=f"{', '.join(people)} reacted with {' '.join(f':{n}:' for n in names)}",
                )
            )
        return shown

    def attachments(self, message: dict) -> list[Attachment]:
        shown = []
        for attachment in _dicts(message.get("attachments")):
            color_text = _text(attachment.get("color"))
            color = COLOR.fullmatch(color_text)
            text = _text(attachment.get("text"))
            image = self.local_image(attachment.get("image_url"))
            title = _text(attachment.get("title"))
            link = attachment.get("title_link")
            icon = attachment.get("service_icon")
            shown.append(
                Attachment(
                    color=f"#{color.group(1)}" if color else NAMED_COLORS.get(color_text),
                    icon=self.assets.picture(icon) if is_web_url(icon) else None,
                    service=_text(attachment.get("service_name"))
                    or _text(attachment.get("author_name")),
                    title=title,
                    link=link if is_web_url(link) else None,
                    text=render_mrkdwn_html(text, self.view, self.assets) if text else Markup(""),
                    image=image,
                    thumb=None if image else self.local_image(attachment.get("thumb_url")),
                    fallback=_text(attachment.get("fallback")) if not (title or text) else "",
                )
            )
        return shown

    def local_image(self, url: object) -> str | None:
        return self.assets.image_src(url) if is_web_url(url) else None

    def file(self, entry: dict) -> FileView:
        file_id = _text(entry.get("id"))
        obj = {**entry, **(self.assets.file_objects.get(file_id) or {})}
        name = _text(obj.get("title")) or _text(obj.get("name")) or file_id or "file"
        mode = obj.get("mode")
        mimetype = _text(obj.get("mimetype"))
        if mode == "tombstone":
            return FileView("tombstone", name)
        if mode == "hidden_by_limit":
            return FileView("hidden_by_limit", name)
        if obj.get("is_external") or mode == "external":
            link = obj.get("external_url")
            return FileView("external", name, link=link if is_web_url(link) else None)
        if mode == "snippet":
            return self.snippet(file_id, obj, name)
        src = self.assets.file_src(file_id)
        if src is None:
            state = self.downloads.get((self.team, file_id))
            return FileView(
                "too_large" if state and state.status == "too_large" else "missing", name
            )
        kind: FileKind = "download"
        for prefix in ("image", "video", "audio"):
            if mimetype.startswith(prefix + "/"):
                kind = prefix
        size = obj.get("size")
        return FileView(kind, name, src, mimetype, _size(size) if isinstance(size, int) else "")

    def snippet(self, file_id: str, obj: dict, name: str) -> FileView:
        """A snippet's full text from its blob, highlighted, or its preview without a blob."""
        blob = self.assets.file_blob(file_id)
        lexer = _lexer(_text(obj.get("name")), _text(obj.get("filetype")))
        if blob is not None:
            text = self.blobs.read(blob).decode("utf-8", errors="replace")
            note = ""
        else:
            text = _text(obj.get("preview"))
            note = "Only the start of this snippet was archived."
        return FileView(
            "snippet", name, code=Markup(highlight(text, lexer, HtmlFormatter())), note=note
        )


def _lexer(filename: str, filetype: str) -> Lexer:
    try:
        return get_lexer_for_filename(filename, stripnl=False)
    except ClassNotFound:
        pass
    try:
        return get_lexer_by_name(filetype, stripnl=False)
    except ClassNotFound:
        return TextLexer(stripnl=False)


def _extension(name: str | None, content_type: str | None) -> str:
    """The asset file extension: name's own suffix, else one for the content type, else bin.
    Page and script extensions become txt so opening the asset cannot run it."""
    suffix = PurePosixPath(name or "").suffix.removeprefix(".").lower()
    if EXTENSION.fullmatch(suffix):
        extension = suffix
    else:
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type or "", "bin")
    return "txt" if extension in ACTIVE_EXTENSIONS else extension


def _image_extension(url: str, content_type: str | None) -> str:
    """The asset extension for an image: from its content type, else an image suffix of its
    URL, else bin."""
    known = CONTENT_TYPE_EXTENSIONS.get(content_type or "")
    if known:
        return known
    suffix = PurePosixPath(urlparse(url).path).suffix.removeprefix(".").lower()
    return suffix if suffix in IMAGE_EXTENSIONS else "bin"


def _clock(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _seconds(ts: str) -> int:
    return int(ts.split(".")[0])


def _parse_iso(value: str) -> int:
    return int(datetime.strptime(value, ISO_SECONDS).replace(tzinfo=UTC).timestamp())


def _size(size: int) -> str:
    if size < 1000:
        return f"{size} bytes"
    if size < 1_000_000:
        return f"{size / 1000:.1f} KB"
    return f"{size / 1_000_000:.1f} MB"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dicts(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
