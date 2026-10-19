# ABOUTME: render_mrkdwn_html renders Slack mrkdwn text as escaped HTML: styles, code, quotes,
# ABOUTME: <...> links and mentions and :emoji:. Also the emoji and link markup the blocks share.
import re

from markupsafe import Markup, escape

from slarc.domain.directory import DirectoryView
from slarc.domain.emoji import SHORTCODE, EmojiRender, resolve_emoji
from slarc.domain.plain import mrkdwn_plain, mrkdwn_token_text, unescape_mrkdwn
from slarc.domain.render import AssetResolver

LINK_SCHEMES = ("https://", "http://", "mailto:")

_HELD = re.compile(
    r"```(?P<pre>.*?)```"
    r"|`(?P<code>[^`\n]+)`"
    r"|<(?P<token>[^<>]+)>"
    rf"|:(?P<emoji>{SHORTCODE}):",
    re.DOTALL,
)
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")
_STYLES = (
    (re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])"), "strong"),
    (re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])"), "em"),
    (re.compile(r"(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])"), "s"),
)
_QUOTE_REST = ("&gt;&gt;&gt;", ">>>")
_QUOTE = ("&gt;", ">")


def render_mrkdwn_html(text: str, view: DirectoryView, assets: AssetResolver) -> Markup:
    """text as HTML. Markup escapes str operands of +, % and format, so compose with care."""
    held: list[Markup] = []
    lines = _hold(text.replace("\x00", ""), view, assets, held).split("\n")
    html = "".join(_group_html(quoted, group) for quoted, group in _quote_groups(lines))
    return Markup(_PLACEHOLDER.sub(lambda m: held[int(m.group(1))], html))


def emoji_html(name: str, render: EmojiRender, titled: bool = True) -> Markup:
    """The emoji's markup, with a :name: tooltip unless titled is False."""
    shortcode = f":{name}:"
    title = Markup(' title="{}"').format(shortcode) if titled else Markup("")
    match render.kind:
        case "unicode":
            return Markup('<span class="emoji"{}>{}</span>').format(title, render.value)
        case "image":
            return Markup('<img class="emoji" src="{}" alt="{}"{}>').format(
                render.value, shortcode, title
            )
        case _:
            return escape(render.value)


def link_html(url: str | None, label: Markup) -> Markup:
    """label linked to url, or label alone when url is not a web or mailto link."""
    if isinstance(url, str) and url.startswith(LINK_SCHEMES):
        return Markup('<a href="{}">{}</a>').format(url, label)
    return label


def mention_html(text: str) -> Markup:
    return Markup('<span class="mention">{}</span>').format(text)


def _hold(text: str, view: DirectoryView, assets: AssetResolver, held: list[Markup]) -> str:
    """text with code, <...> tokens and known :emoji: rendered into held and replaced by
    placeholders, so the style markers inside them stay literal."""
    out: list[str] = []
    pos = 0
    while match := _HELD.search(text, pos):
        out.append(text[pos : match.start()])
        fragment = _fragment(match, view, assets)
        if fragment is None:
            out.append(text[match.start()])
            pos = match.start() + 1
            continue
        held.append(fragment)
        out.append(f"\x00{len(held) - 1}\x00")
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def _fragment(match: re.Match[str], view: DirectoryView, assets: AssetResolver) -> Markup | None:
    """The HTML for one held match, or None for an unknown :name: that stays as text."""
    if (pre := match.group("pre")) is not None:
        return Markup("<pre>{}</pre>").format(mrkdwn_plain(pre, view))
    if (code := match.group("code")) is not None:
        return Markup("<code>{}</code>").format(mrkdwn_plain(code, view))
    if (token := match.group("token")) is not None:
        return _token_html(token, view)
    name = match.group("emoji")
    render = resolve_emoji(name, view, assets.emoji_src)
    return None if render.kind == "text" else emoji_html(name, render)


def _token_html(token: str, view: DirectoryView) -> Markup:
    target, _, label = token.partition("|")
    if target.startswith("!date^"):
        return escape(mrkdwn_token_text(token, view))
    if target.startswith(("@", "#", "!")):
        return mention_html(mrkdwn_token_text(token, view))
    return link_html(unescape_mrkdwn(target), escape(unescape_mrkdwn(label or target)))


def _quote_groups(lines: list[str]) -> list[tuple[bool, list[str]]]:
    """Consecutive lines grouped by whether they are quoted, with the quote markers removed.
    A line starting with >>> quotes the rest of the text."""
    groups: list[tuple[bool, list[str]]] = []
    for index, line in enumerate(lines):
        if line.startswith(_QUOTE_REST):
            rest = [_strip_marker(line, _QUOTE_REST), *lines[index + 1 :]]
            if groups and groups[-1][0]:
                groups[-1][1].extend(rest)
            else:
                groups.append((True, rest))
            break
        quoted = line.startswith(_QUOTE)
        content = _strip_marker(line, _QUOTE) if quoted else line
        if groups and groups[-1][0] == quoted:
            groups[-1][1].append(content)
        else:
            groups.append((quoted, [content]))
    return groups


def _strip_marker(line: str, markers: tuple[str, ...]) -> str:
    marker = next(m for m in markers if line.startswith(m))
    rest = line.removeprefix(marker)
    return rest.removeprefix(" ")


def _group_html(quoted: bool, lines: list[str]) -> str:
    html = "<br>".join(_styled(line) for line in lines)
    return f"<blockquote>{html}</blockquote>" if quoted else html


def _styled(line: str) -> str:
    html = str(escape(unescape_mrkdwn(line)))
    for pattern, tag in _STYLES:
        html = pattern.sub(rf"<{tag}>\1</{tag}>", html)
    return html
