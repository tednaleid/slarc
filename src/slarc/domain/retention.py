# ABOUTME: Retention values, which retention applies to a tracked item, and the cutoff it sets.
# ABOUTME: A message's age is that of the newest stored message of its thread.
import re
from datetime import datetime, timedelta
from typing import Literal

from slarc.domain.envelope import Envelope
from slarc.domain.links import MessageTs

type Retention = int | Literal["forever"]

FOREVER: Literal["forever"] = "forever"
RETENTION = re.compile(r"([1-9]\d*)([dw])")
FORMS = "--retention must be a number of days or weeks, such as 30d or 12w, or forever."


def parse_retention(text: str) -> Retention:
    """Days from 30d or 12w, or forever; raises ValueError for anything else."""
    if text == FOREVER:
        return FOREVER
    match = RETENTION.fullmatch(text)
    if match is None:
        raise ValueError(FORMS)
    days = int(match.group(1))
    return days * 7 if match.group(2) == "w" else days


def retention_text(retention: Retention) -> str:
    return FOREVER if retention == FOREVER else f"{retention}d"


def effective_retention(own: Retention | None, workspace: Retention | None) -> Retention:
    """The item's own retention, else its workspace's, else forever."""
    if own is not None:
        return own
    return workspace if workspace is not None else FOREVER


def retention_source(
    own: Retention | None, workspace: Retention | None
) -> Literal["item", "workspace"] | None:
    if own is not None:
        return "item"
    return "workspace" if workspace is not None else None


def cutoff(retention: Retention, now: datetime) -> MessageTs | None:
    """The ts a thread's newest message must be after to be kept, or None to keep everything."""
    if retention == FOREVER:
        return None
    return MessageTs(f"{(now - timedelta(days=retention)).timestamp():.6f}")


def thread_activity(versions: dict[str, list[Envelope]]) -> dict[str, MessageTs]:
    """The newest stored ts of each stored message's thread, or its own ts outside a thread."""
    roots = {ts: _thread_root(ts, lines) for ts, lines in versions.items()}
    newest: dict[str, MessageTs] = {}
    for ts, root in roots.items():
        if root not in newest or MessageTs(ts) > newest[root]:
            newest[root] = MessageTs(ts)
    return {ts: newest[root] for ts, root in roots.items()}


def _thread_root(ts: str, lines: list[Envelope]) -> str:
    for env in lines:
        thread_ts = (env.object or {}).get("thread_ts")
        if thread_ts:
            return thread_ts
    return ts
