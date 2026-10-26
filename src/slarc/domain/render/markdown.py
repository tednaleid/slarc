# ABOUTME: render_message_markdown renders a message body as markdown for reading as text:
# ABOUTME: blocks first, else the mrkdwn text, with mentions resolved and emoji as :name:.
import re
from datetime import UTC, datetime

from slarc.domain.directory import DirectoryView
from slarc.domain.links import is_web_url
from slarc.domain.plain import mention_text, mrkdwn_token_text, unescape_mrkdwn
from slarc.domain.render import dict_list, fallback_text, text_field
from slarc.domain.render.blocks import RENDERED_SUBTYPES, toned_emoji_name
from slarc.domain.render.lists import RichList, nest_lists
from slarc.domain.render.mrkdwn import LINK_SCHEMES

# Innermost first, so code sits inside the other markers.
_STYLE_MARKS = (("code", "`"), ("strike", "~~"), ("bold", "**"), ("italic", "_"))
_HELD = re.compile(r"```.*?```|`[^`\n]+`|<(?P<token>[^<>]+)>", re.DOTALL)
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")
_MRKDWN_STYLES = (
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"), r"**\1**"),
    (re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])"), r"~~\1~~"),
)


def render_message_markdown(message: dict, view: DirectoryView) -> str:
    """The message body as markdown. The body excludes attachments, files, reactions and the
    author, time and thread around the message."""
    renderer = _Renderer(view)
    subtype = message.get("subtype")
    text = text_field(message.get("text"))
    if subtype is not None and subtype not in RENDERED_SUBTYPES:
        return mrkdwn_markdown(text, view) if text else text_field(subtype)
    blocks = [block for block in message.get("blocks") or [] if isinstance(block, dict)]
    if blocks:
        return "\n".join(part for part in map(renderer.block, blocks) if part)
    return mrkdwn_markdown(text, view)


def mrkdwn_markdown(text: str, view: DirectoryView) -> str:
    """Slack mrkdwn as markdown: *bold* and ~strike~ take markdown's markers, <...> tokens
    become links and names, > and >>> quote, and code keeps its text as it is."""
    held: list[str] = []

    def hold(match: re.Match[str]) -> str:
        token = match.group("token")
        held.append(_token(token, view) if token is not None else unescape_mrkdwn(match.group()))
        return f"\x00{len(held) - 1}\x00"

    lines = _HELD.sub(hold, text.replace("\x00", "")).split("\n")
    out = []
    quoting = False
    for line in lines:
        if line.startswith((">>>", "&gt;&gt;&gt;")):
            quoting = True
            line = line.removeprefix(">>>").removeprefix("&gt;&gt;&gt;").removeprefix(" ")
        elif line.startswith(("&gt;", ">")) and not quoting:
            line = "> " + line.removeprefix("&gt;").removeprefix(">").removeprefix(" ")
        styled = unescape_mrkdwn(line)
        for pattern, replacement in _MRKDWN_STYLES:
            styled = pattern.sub(replacement, styled)
        out.append(f"> {styled}" if quoting else styled)
    return _PLACEHOLDER.sub(lambda m: held[int(m.group(1))], "\n".join(out))


class _Renderer:
    def __init__(self, view: DirectoryView) -> None:
        self.view = view

    def block(self, block: dict) -> str:
        match block.get("type"):
            case "rich_text":
                return self.rich_text(dict_list(block.get("elements")))
            case "section":
                fields = [self.text_object(f) for f in dict_list(block.get("fields"))]
                return "\n".join(p for p in (self.text_object(block.get("text")), *fields) if p)
            case "header":
                header = self.text_object(block.get("text"))
                return f"**{header}**" if header else ""
            case "divider":
                return "---"
            case "image":
                return self.image(block)
            case "context":
                parts = [self.context_element(e) for e in dict_list(block.get("elements"))]
                return " ".join(part for part in parts if part)
            case "actions":
                return " ".join(
                    f"[{self.text_object(e['text'])}]"
                    for e in dict_list(block.get("elements"))
                    if isinstance(e.get("text"), dict)
                )
            case _:
                return fallback_text(block)

    def text_object(self, obj: object) -> str:
        if not isinstance(obj, dict):
            return ""
        text = text_field(obj.get("text"))
        return mrkdwn_markdown(text, self.view) if obj.get("type") == "mrkdwn" else text

    def image(self, block: dict) -> str:
        url = text_field(block.get("image_url"))
        alt = text_field(block.get("alt_text")) or self.text_object(block.get("title"))
        return f"[image: {alt}]({url})" if is_web_url(url) else f"[image: {alt}]"

    def context_element(self, element: dict) -> str:
        match element.get("type"):
            case "image":
                return text_field(element.get("alt_text"))
            case "mrkdwn" | "plain_text":
                return self.text_object(element)
            case _:
                return fallback_text(element)

    def rich_text(self, containers: list[dict]) -> str:
        parts: list[str] = []
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
        return "\n".join(parts)

    def container(self, container: dict) -> str:
        elements = dict_list(container.get("elements"))
        match container.get("type"):
            case "rich_text_section":
                return self.inline(elements).removesuffix("\n")
            case "rich_text_quote":
                return "\n".join(f"> {line}" for line in self.inline(elements).split("\n"))
            case "rich_text_preformatted":
                code = "".join(text_field(e.get("text")) or fallback_text(e) for e in elements)
                return f"```\n{code.removesuffix(chr(10))}\n```"
            case _:
                return fallback_text(container)

    def lists(self, run: list[dict]) -> str:
        return "\n".join(_list_lines(root, "") for root in nest_lists(run, self.inline))

    def inline(self, elements: list[dict]) -> str:
        return "".join(self.leaf(element) for element in elements)

    def leaf(self, element: dict) -> str:
        match element.get("type"):
            case "text":
                return _styled(text_field(element.get("text")), element.get("style"))
            case "link":
                url = text_field(element.get("url"))
                label = text_field(element.get("text"))
                return _styled(_link(url, label), element.get("style"))
            case "emoji":
                return f":{toned_emoji_name(element)}:"
            case "user" | "channel" | "usergroup" | "broadcast":
                return mention_text(element, self.view)
            case "date" if isinstance(element.get("timestamp"), int):
                return _date(element)
            case "color":
                return text_field(element.get("value"))
            case _:
                return fallback_text(element)


def _list_lines(rich_list: RichList[str], indent: str) -> str:
    lines = []
    for number, item in enumerate(rich_list.items, start=rich_list.offset + 1):
        marker = f"{number}. " if rich_list.style == "ordered" else "- "
        lines.append(f"{indent}{marker}{item.content}")
        lines += [_list_lines(child, indent + " " * len(marker)) for child in item.children]
    return "\n".join(lines)


def _token(token: str, view: DirectoryView) -> str:
    target, _, label = token.partition("|")
    if target.startswith(("@", "#", "!")):
        return mrkdwn_token_text(token, view)
    return _link(unescape_mrkdwn(target), unescape_mrkdwn(label))


def _link(url: str, label: str) -> str:
    """A markdown link, the bare URL when it has no other label, or the label alone when the
    URL is not a web or mailto link."""
    if not url.startswith(LINK_SCHEMES):
        return label
    return f"[{label}]({url})" if label and label != url else url


def _styled(text: str, style: object) -> str:
    if not isinstance(style, dict) or not text.strip():
        return text
    for flag, mark in _STYLE_MARKS:
        if style.get(flag):
            text = f"{mark}{text}{mark}"
    return text


def _date(element: dict) -> str:
    fallback = text_field(element.get("fallback"))
    try:
        moment = datetime.fromtimestamp(element["timestamp"], UTC)
    except OSError, OverflowError, ValueError:
        return fallback
    return fallback or moment.strftime("%Y-%m-%dT%H:%M:%SZ")
