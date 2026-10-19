# ABOUTME: Finds the threads a conversation pull re-reads and the messages a re-read shows deleted.
# ABOUTME: Reply discovery uses search.messages permalinks; delete inference is a pure function.
from datetime import datetime, timedelta

from slarc.domain.links import MessageSel, MessageTs, parse_selector
from slarc.ports import SlackGateway, StoredMessage


def discover_reply_threads(gateway: SlackGateway, channel: str, since_date: str) -> set[str]:
    """The thread_ts of each thread in channel with a message after the YYYY-MM-DD date.

    Matches carry no thread_ts field, but the permalink of a reply or thread parent does.
    """
    found = set()
    for match in gateway.search(f"in:<#{channel}> after:{since_date}"):
        try:
            selector = parse_selector(match.get("permalink") or "")
        except ValueError:
            continue
        if isinstance(selector, MessageSel) and selector.channel == channel and selector.thread_ts:
            found.add(str(selector.thread_ts))
    return found


def active_threads(stored: dict[str, StoredMessage], now: datetime, days: int) -> set[str]:
    """The thread_ts of each thread with a stored reply newer than days before now."""
    cutoff = MessageTs(f"{int((now - timedelta(days=days)).timestamp())}.000000")
    return {
        message.thread_ts
        for ts, message in stored.items()
        if message.thread_ts is not None and message.thread_ts != ts and MessageTs(ts) > cutoff
    }


def infer_deletes(
    stored_in_range: dict[str, StoredMessage],
    returned: set[str],
    range_start: str | None,
    oldest_returned: str | None,
) -> set[str]:
    """The stored, not yet deleted messages of a fully re-read range that Slack did not return.

    Slack may stop serving the oldest messages, so only those at or after the oldest message
    returned count, and none count when Slack returned nothing. range_start is exclusive.
    """
    if oldest_returned is None:
        return set()
    return {
        ts
        for ts, message in stored_in_range.items()
        if message.hash is not None
        and ts not in returned
        and MessageTs(ts) >= oldest_returned
        and (range_start is None or MessageTs(ts) > range_start)
    }
