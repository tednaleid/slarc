# ABOUTME: Parses Slack permalinks and command-line selectors into typed values.
# ABOUTME: Also builds message permalinks, defines the orderable MessageTs and spots file URLs.
import re
from dataclasses import dataclass
from typing import Self, TypeIs
from urllib.parse import parse_qs, urlparse

CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]{8,}")
FILE_ID = re.compile(r"F[A-Z0-9]{8,}")
WORKSPACE_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*")
PERMALINK_DIGITS = re.compile(r"p(\d{16})")
SLACK_HOST_SUFFIX = ".slack.com"
SLACK_FILE_HOST = "files.slack.com"


class MessageTs(str):
    """A Slack message timestamp such as "1792410693.663869", ordered numerically."""

    @classmethod
    def from_permalink_digits(cls, digits: str) -> Self:
        return cls(f"{digits[:-6]}.{digits[-6:]}")

    def _key(self) -> tuple[int, int]:
        seconds, _, micros = self.partition(".")
        return int(seconds), int(micros or 0)

    def __lt__(self, other: str) -> bool:
        return self._key() < MessageTs(other)._key()

    def __le__(self, other: str) -> bool:
        return self._key() <= MessageTs(other)._key()

    def __gt__(self, other: str) -> bool:
        return self._key() > MessageTs(other)._key()

    def __ge__(self, other: str) -> bool:
        return self._key() >= MessageTs(other)._key()


@dataclass(frozen=True)
class ConversationSel:
    domain: str | None
    channel: str | None
    name: str | None
    dm_user: str | None


@dataclass(frozen=True)
class MessageSel:
    domain: str | None
    channel: str
    ts: MessageTs
    thread_ts: MessageTs | None


@dataclass(frozen=True)
class WorkspaceSel:
    domain: str


Selector = ConversationSel | MessageSel | WorkspaceSel


def parse_selector(text: str) -> Selector:
    text = text.strip()
    if text.startswith(("http://", "https://")):
        return _parse_link(text)
    if text.startswith("#") and len(text) > 1:
        return ConversationSel(domain=None, channel=None, name=text[1:], dm_user=None)
    if text.startswith("@") and len(text) > 1:
        return ConversationSel(domain=None, channel=None, name=None, dm_user=text[1:])
    if CHANNEL_ID.fullmatch(text):
        return ConversationSel(domain=None, channel=text, name=None, dm_user=None)
    if WORKSPACE_DOMAIN.fullmatch(text.lower()):
        return WorkspaceSel(domain=text.lower())
    raise ValueError(
        f"Cannot read '{text}' as a selector. Use a Slack link, a channel id, "
        "#channel-name, @user or a workspace name."
    )


def parse_file_selector(text: str) -> tuple[str, str | None]:
    """The file id, and the workspace domain when given, of a file id such as F0123456789 or a
    file's Slack link, https://<workspace>.slack.com/files/<user>/<file id>[/<name>]."""
    if FILE_ID.fullmatch(text):
        return text, None
    parts = urlparse(text)
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
    if (
        parts.scheme == "https"
        and host.endswith(SLACK_HOST_SUFFIX)
        and host != SLACK_HOST_SUFFIX[1:]
        and len(segments) in (3, 4)
        and segments[0] == "files"
        and FILE_ID.fullmatch(segments[2])
    ):
        return segments[2], host.removesuffix(SLACK_HOST_SUFFIX)
    raise ValueError(text)


def _parse_link(url: str) -> Selector:
    parts = urlparse(url)
    host = parts.hostname or ""
    segments = [s for s in parts.path.split("/") if s]
    if (
        not host.endswith(SLACK_HOST_SUFFIX)
        or host == SLACK_HOST_SUFFIX[1:]
        or len(segments) not in (2, 3)
        or segments[0] != "archives"
        or not CHANNEL_ID.fullmatch(segments[1])
    ):
        raise ValueError(
            f"'{url}' is not a Slack link. Expected "
            "https://<workspace>.slack.com/archives/<channel id>[/p<digits>]."
        )
    domain = host.removesuffix(SLACK_HOST_SUFFIX)
    channel = segments[1]
    if len(segments) == 2:
        return ConversationSel(domain=domain, channel=channel, name=None, dm_user=None)
    match = PERMALINK_DIGITS.fullmatch(segments[2])
    if not match:
        raise ValueError(
            f"'{segments[2]}' in '{url}' is not a message id. It must be 'p' followed by 16 digits."
        )
    thread_values = parse_qs(parts.query).get("thread_ts")
    return MessageSel(
        domain=domain,
        channel=channel,
        ts=MessageTs.from_permalink_digits(match.group(1)),
        thread_ts=MessageTs(thread_values[0]) if thread_values else None,
    )


def message_link(domain: str, channel: str, ts: str, thread_ts: str | None = None) -> str:
    link = f"https://{domain}{SLACK_HOST_SUFFIX}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts:
        link += f"?thread_ts={thread_ts}&cid={channel}"
    return link


def is_web_url(url: object) -> TypeIs[str]:
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def is_slack_file_url(url: str) -> bool:
    """True for a private file URL on Slack's file host, the only URLs that need the session."""
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == SLACK_FILE_HOST
    except ValueError:
        return False
