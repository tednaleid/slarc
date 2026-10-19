# ABOUTME: plain_text renders a Slack message as readable text for meta.plain.
# ABOUTME: Resolves mentions, channels, emoji and links through a DirectoryView; snippet cuts it.
import re

from slarc.domain.directory import DirectoryView

_MRKDWN_TOKEN = re.compile(r"<([^<>]+)>")
SNIPPET_WIDTH = 120
ELLIPSIS = "..."


def plain_text(message: dict, view: DirectoryView) -> str:
    blocks = message.get("blocks") or []
    rich = "\n".join(
        _rich_text_block(block, view) for block in blocks if block.get("type") == "rich_text"
    )
    body = rich or mrkdwn_plain(message.get("text") or "", view)
    parts = [body, *_layout_parts(blocks, view)]
    parts += [a["title"] for a in message.get("attachments") or [] if a.get("title")]
    parts += [f["name"] for f in message.get("files") or [] if f.get("name")]

    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return f"{view.author_display(message)}: {' '.join(unique)}".rstrip()


def unescape_mrkdwn(text: str) -> str:
    """text with the three entities Slack escapes in mrkdwn turned back into characters."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def mrkdwn_plain(text: str, view: DirectoryView) -> str:
    """mrkdwn as plain text: <...> tokens become their labels and entities are unescaped."""
    return unescape_mrkdwn(_MRKDWN_TOKEN.sub(lambda m: mrkdwn_token_text(m.group(1), view), text))


def mrkdwn_token_text(token: str, view: DirectoryView) -> str:
    """The text for the inside of a <...> token: a mention's name, or a link's label or URL."""
    target, _, label = token.partition("|")
    if target.startswith("@"):
        return "@" + view.user_display(target[1:])
    if target.startswith("#"):
        return "#" + (label or view.conversation_name(target[1:]))
    if target.startswith("!subteam^"):
        return label or "@" + view.usergroup_handle(target.removeprefix("!subteam^"))
    if target.startswith("!"):
        return label or "@" + target[1:]
    return label or target


def _rich_text_block(block: dict, view: DirectoryView) -> str:
    return "\n".join(_rich_container(element, view) for element in block.get("elements") or [])


def _rich_container(element: dict, view: DirectoryView) -> str:
    if element.get("type") == "rich_text_list":
        return "\n".join(_rich_container(item, view) for item in element.get("elements") or [])
    return "".join(_rich_inline(item, view) for item in element.get("elements") or [])


def _rich_inline(element: dict, view: DirectoryView) -> str:
    match element.get("type"):
        case "text":
            return element.get("text", "")
        case "link":
            return element.get("text") or element.get("url", "")
        case "user" | "channel" | "usergroup" | "broadcast":
            return mention_text(element, view)
        case "emoji":
            return f":{element.get('name', '')}:"
        case _:
            return ""


def mention_text(element: dict, view: DirectoryView) -> str:
    """The text for a rich_text user, channel, usergroup or broadcast element."""
    match element.get("type"):
        case "user":
            return "@" + view.user_display(element.get("user_id", ""))
        case "channel":
            return "#" + view.conversation_name(element.get("channel_id", ""))
        case "usergroup":
            return "@" + view.usergroup_handle(element.get("usergroup_id", ""))
        case _:
            return "@" + element.get("range", "")


def _text_object(obj: dict | None, view: DirectoryView) -> str:
    if not obj:
        return ""
    text = obj.get("text", "")
    return mrkdwn_plain(text, view) if obj.get("type") == "mrkdwn" else text


def _layout_parts(blocks: list[dict], view: DirectoryView) -> list[str]:
    parts: list[str] = []
    for block in blocks:
        match block.get("type"):
            case "section" | "header":
                parts.append(_text_object(block.get("text"), view))
            case "context":
                parts += [
                    _text_object(e, view)
                    for e in block.get("elements") or []
                    if e.get("type") in ("mrkdwn", "plain_text")
                ]
            case "image":
                parts.append(block.get("alt_text", ""))
    return parts


def snippet(plain: str, text: str, width: int = SNIPPET_WIDTH) -> str:
    """plain on one line, cut to width characters around the first case-insensitive match."""
    line = " ".join(plain.split())
    if len(line) <= width:
        return line
    needle = " ".join(text.split()).lower()
    found = line.lower().find(needle)
    margin = max(0, (width - len(needle)) // 2)
    start = 0 if found < 0 else max(0, min(found - margin, len(line) - width))
    end = start + width
    return (ELLIPSIS if start else "") + line[start:end] + (ELLIPSIS if end < len(line) else "")
