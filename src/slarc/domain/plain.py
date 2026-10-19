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
    body = rich or _mrkdwn(message.get("text") or "", view)
    parts = [body, *_layout_parts(blocks, view)]
    parts += [a["title"] for a in message.get("attachments") or [] if a.get("title")]
    parts += [f["name"] for f in message.get("files") or [] if f.get("name")]

    unique: list[str] = []
    for part in parts:
        if part and part not in unique:
            unique.append(part)
    return f"{view.author_display(message)}: {' '.join(unique)}".rstrip()


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _mrkdwn(text: str, view: DirectoryView) -> str:
    return _unescape(_MRKDWN_TOKEN.sub(lambda m: _mrkdwn_token(m.group(1), view), text))


def _mrkdwn_token(token: str, view: DirectoryView) -> str:
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
        case "user":
            return "@" + view.user_display(element.get("user_id", ""))
        case "channel":
            return "#" + view.conversation_name(element.get("channel_id", ""))
        case "usergroup":
            return "@" + view.usergroup_handle(element.get("usergroup_id", ""))
        case "broadcast":
            return "@" + element.get("range", "")
        case "emoji":
            return f":{element.get('name', '')}:"
        case _:
            return ""


def _text_object(obj: dict | None, view: DirectoryView) -> str:
    if not obj:
        return ""
    text = obj.get("text", "")
    return _mrkdwn(text, view) if obj.get("type") == "mrkdwn" else text


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
