# ABOUTME: render_message_html renders a message body as escaped HTML: blocks first, else the
# ABOUTME: mrkdwn text, and a muted generic message for subtypes outside the rendered allowlist.
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

from markupsafe import Markup, escape

from slarc.domain.directory import DirectoryView
from slarc.domain.emoji import EmojiRender, code_points_text, resolve_emoji
from slarc.domain.links import is_web_url
from slarc.domain.plain import mention_text
from slarc.domain.render import AssetResolver
from slarc.domain.render.mrkdwn import emoji_html, link_html, mention_html, render_mrkdwn_html

RENDERED_SUBTYPES = frozenset(
    {
        "bot_message",
        "me_message",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "thread_broadcast",
        "pinned_item",
        "file_share",
        "tombstone",
        "huddle_thread",
    }
)
_STYLE_TAGS = (
    ("code", "code"),
    ("highlight", "mark"),
    ("underline", "u"),
    ("strike", "s"),
    ("italic", "em"),
    ("bold", "strong"),
)
_EMPTY = Markup("")
_NEWLINE = Markup("\n")


def render_message_html(message: dict, view: DirectoryView, assets: AssetResolver) -> str:
    """The message body as HTML, returned as markupsafe Markup. The body excludes attachments,
    files, reactions, the edit marker and the author, time and thread around the message."""
    renderer = _Renderer(view, assets)
    subtype = message.get("subtype")
    text = message.get("text") or ""
    if subtype is not None and subtype not in RENDERED_SUBTYPES:
        body = renderer.mrkdwn(text) if text else escape(subtype)
        return Markup('<div class="generic-message">{}</div>').format(body)
    blocks = [block for block in message.get("blocks") or [] if isinstance(block, dict)]
    if blocks:
        return _NEWLINE.join(renderer.block(block) for block in blocks)
    if not text:
        return _EMPTY
    return Markup('<div class="mrkdwn">{}</div>').format(renderer.mrkdwn(text))


@dataclass
class _Item:
    content: Markup
    children: list[_List] = field(default_factory=list)


@dataclass
class _List:
    style: str
    indent: int
    offset: int
    items: list[_Item]


@dataclass(frozen=True)
class _Renderer:
    view: DirectoryView
    assets: AssetResolver

    def mrkdwn(self, text: str) -> Markup:
        return Markup(render_mrkdwn_html(text, self.view, self.assets))

    def block(self, block: dict) -> Markup:
        match block.get("type"):
            case "rich_text":
                return self.rich_text(_dicts(block.get("elements")))
            case "section":
                fields = _dicts(block.get("fields"))
                fields_html = (
                    Markup('<div class="fields">{}</div>').format(
                        _EMPTY.join(
                            Markup("<div>{}</div>").format(self.text_object(f)) for f in fields
                        )
                    )
                    if fields
                    else _EMPTY
                )
                return Markup('<div class="section">{}{}</div>').format(
                    self.text_object(block.get("text")), fields_html
                )
            case "header":
                return Markup('<div class="header">{}</div>').format(
                    self.text_object(block.get("text"))
                )
            case "divider":
                return Markup("<hr>")
            case "image":
                return self.image_block(block)
            case "context":
                return Markup('<div class="context">{}</div>').format(
                    Markup(" ").join(self.context_element(e) for e in _dicts(block.get("elements")))
                )
            case "actions":
                labels = [
                    Markup('<span class="action">{}</span>').format(self.text_object(e["text"]))
                    for e in _dicts(block.get("elements"))
                    if isinstance(e.get("text"), dict)
                ]
                return Markup('<div class="actions">{}</div>').format(_EMPTY.join(labels))
            case _:
                return _unknown("div", block)

    def text_object(self, obj: object) -> Markup:
        if not isinstance(obj, dict):
            return _EMPTY
        text = obj.get("text") or ""
        return self.mrkdwn(text) if obj.get("type") == "mrkdwn" else escape(text)

    def image_block(self, block: dict) -> Markup:
        url = block.get("image_url")
        alt = block.get("alt_text") or ""
        src = self.assets.image_src(url) if is_web_url(url) else None
        if src:
            width, height = block.get("image_width"), block.get("image_height")
            size = (
                Markup(' width="{}" height="{}"').format(width, height)
                if isinstance(width, int) and isinstance(height, int)
                else _EMPTY
            )
            image = Markup('<img src="{}" alt="{}"{}>').format(src, alt, size)
        else:
            image = link_html(url, escape(alt or url or ""))
        title = self.text_object(block.get("title"))
        caption = Markup("<figcaption>{}</figcaption>").format(title) if title else _EMPTY
        return Markup('<figure class="image">{}{}</figure>').format(image, caption)

    def context_element(self, element: dict) -> Markup:
        match element.get("type"):
            case "image":
                url = element.get("image_url")
                alt = element.get("alt_text") or ""
                src = self.assets.image_src(url) if is_web_url(url) else None
                if src is None:
                    return escape(alt)
                return Markup('<img class="context-image" src="{}" alt="{}">').format(src, alt)
            case "mrkdwn" | "plain_text":
                return Markup("<span>{}</span>").format(self.text_object(element))
            case _:
                return _unknown("span", element)

    def rich_text(self, containers: list[dict]) -> Markup:
        parts: list[Markup] = []
        lists: list[dict] = []
        for container in containers:
            if container.get("type") == "rich_text_list":
                lists.append(container)
                continue
            if lists:
                parts.append(self.lists(lists))
                lists = []
            parts.append(self.container(container))
        if lists:
            parts.append(self.lists(lists))
        return _NEWLINE.join(parts)

    def container(self, container: dict) -> Markup:
        elements = _dicts(container.get("elements"))
        match container.get("type"):
            case "rich_text_section":
                return Markup("<p>{}</p>").format(self.inline(elements))
            case "rich_text_quote":
                return Markup("<blockquote>{}</blockquote>").format(self.inline(elements))
            case "rich_text_preformatted":
                return Markup("<pre>{}</pre>").format(self.inline(elements, preformatted=True))
            case _:
                return _unknown("div", container)

    def lists(self, run: list[dict]) -> Markup:
        """Consecutive rich_text_list containers as nested lists. Slack sends each indent level
        as its own container, so a deeper one nests under the last item of the one before."""
        roots: list[_List] = []
        open_lists: list[_List] = []
        for container in run:
            indent = container.get("indent") or 0
            style = container.get("style") or "bullet"
            items = [
                _Item(self.inline(_dicts(item.get("elements"))))
                for item in _dicts(container.get("elements"))
            ]
            while open_lists and open_lists[-1].indent > indent:
                open_lists.pop()
            if open_lists and open_lists[-1].indent == indent:
                if open_lists[-1].style == style:
                    open_lists[-1].items += items
                    continue
                open_lists.pop()
            current = _List(style, indent, container.get("offset") or 0, items)
            if open_lists and open_lists[-1].items:
                open_lists[-1].items[-1].children.append(current)
            else:
                roots.append(current)
            open_lists.append(current)
        return _NEWLINE.join(_list_html(root) for root in roots)

    def inline(self, elements: list[dict], preformatted: bool = False) -> Markup:
        return _EMPTY.join(self.leaf(element, preformatted) for element in elements)

    def leaf(self, element: dict, preformatted: bool) -> Markup:
        match element.get("type"):
            case "text":
                text = element.get("text") or ""
                content = escape(text) if preformatted else _with_breaks(text)
                return _styled(content, element.get("style"))
            case "link":
                url = element.get("url")
                label = _styled(
                    _with_breaks(element.get("text") or url or ""), element.get("style")
                )
                return link_html(url, label)
            case "emoji":
                return self.emoji(element)
            case "user" | "channel" | "usergroup" | "broadcast":
                return mention_html(mention_text(element, self.view))
            case "date" if isinstance(element.get("timestamp"), int):
                moment = datetime.fromtimestamp(element["timestamp"], UTC)
                iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
                return Markup('<time datetime="{}">{}</time>').format(
                    iso, element.get("fallback") or iso
                )
            case "color":
                return escape(element.get("value") or "")
            case _:
                return _unknown("span", element)

    def emoji(self, element: dict) -> Markup:
        name = element.get("name") or ""
        render = resolve_emoji(name, self.view, self.assets.emoji_src)
        unicode = element.get("unicode")
        if render.kind == "text" and isinstance(unicode, str):
            with suppress(ValueError):
                render = EmojiRender("unicode", code_points_text(unicode))
        return emoji_html(name, render)


def _dicts(value: object) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _with_breaks(text: str) -> Markup:
    return Markup("<br>").join(escape(line) for line in text.split("\n"))


def _styled(content: Markup, style: object) -> Markup:
    if not isinstance(style, dict):
        return content
    for flag, tag in _STYLE_TAGS:
        if style.get(flag):
            content = Markup(f"<{tag}>{{}}</{tag}>").format(content)
    return content


def _list_html(rich_list: _List) -> Markup:
    tag = "ol" if rich_list.style == "ordered" else "ul"
    start = (
        Markup(' start="{}"').format(rich_list.offset + 1)
        if tag == "ol" and rich_list.offset
        else _EMPTY
    )
    items = _EMPTY.join(
        Markup("<li>{}{}</li>").format(
            item.content, _EMPTY.join(_list_html(child) for child in item.children)
        )
        for item in rich_list.items
    )
    return Markup(f"<{tag}{{}}>{{}}</{tag}>").format(start, items)


def _unknown(tag: str, obj: dict) -> Markup:
    """An element or block of a type the renderer does not know, as its escaped text."""
    return Markup(f'<{tag} class="unknown">{{}}</{tag}>').format(_fallback_text(obj))


def _fallback_text(obj: dict) -> str:
    text = obj.get("text")
    if isinstance(text, dict):
        text = text.get("text")
    for candidate in (text, obj.get("fallback"), obj.get("url")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""
