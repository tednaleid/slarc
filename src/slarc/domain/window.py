# ABOUTME: Window, the span of posting times --since and --until give, as exclusive ts bounds.
# ABOUTME: parse_bound reads a date, a time with an optional offset, or an age such as 7d.
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Self

from slarc.domain.links import MessageTs

AGE = re.compile(r"(\d+)([hdw])")
AGE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?")
FORMS = "must be a date, a time or an age such as 2026-10-19, 2026-10-19T14:30 or 7d."


def parse_bound(text: str, now: datetime, zone: tzinfo, end: bool) -> datetime:
    """The moment text names: an age counts back from now, and a date or time without an
    offset is read in zone. With end, a date alone means the start of the next day, so the
    window runs through that whole day."""
    age = AGE.fullmatch(text)
    if age:
        return now - timedelta(**{AGE_UNITS[age.group(2)]: int(age.group(1))})
    try:
        if DATE.fullmatch(text):
            start = datetime.fromisoformat(text).replace(tzinfo=zone)
            return start + timedelta(days=1) if end else start
        if TIME.fullmatch(text):
            moment = datetime.fromisoformat(text)
            return moment if moment.tzinfo else moment.replace(tzinfo=zone)
    except ValueError:
        pass
    raise ValueError(FORMS)


@dataclass(frozen=True)
class Window:
    """Messages with since < ts < until, compared as MessageTs; a missing bound leaves that
    side open."""

    since: str | None = None
    until: str | None = None

    @classmethod
    def parse(cls, since: str | None, until: str | None, now: datetime, zone: tzinfo) -> Self:
        bounds = []
        for flag, text, end in (("--since", since, False), ("--until", until, True)):
            try:
                moment = parse_bound(text, now, zone, end) if text is not None else None
            except ValueError as error:
                raise ValueError(f"{flag} {error}") from None
            bounds.append(f"{moment.timestamp():.6f}" if moment else None)
        start, stop = bounds
        if start is not None and stop is not None and MessageTs(start) >= stop:
            raise ValueError("--since must be before --until.")
        return cls(start, stop)

    def contains(self, ts: str) -> bool:
        return (self.since is None or MessageTs(ts) > self.since) and (
            self.until is None or MessageTs(ts) < self.until
        )


# A window with both sides open.
OPEN_WINDOW = Window()
