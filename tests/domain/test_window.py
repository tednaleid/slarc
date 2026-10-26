# ABOUTME: Tests for the window grammar: dates, times, offsets and ages for --since and --until.
# ABOUTME: Local values are read in a given zone; a date-only end runs through that whole day.
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from slarc.domain.window import Window, parse_bound

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 26, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "end", "expected"),
    [
        ("2026-10-20", False, datetime(2026, 10, 20, tzinfo=CHICAGO)),
        ("2026-10-20", True, datetime(2026, 10, 21, tzinfo=CHICAGO)),
        ("2026-10-20T14:30", True, datetime(2026, 10, 20, 14, 30, tzinfo=CHICAGO)),
        ("2026-10-20T14:30:15", False, datetime(2026, 10, 20, 14, 30, 15, tzinfo=CHICAGO)),
        ("2026-10-20T14:30Z", False, datetime(2026, 10, 20, 14, 30, tzinfo=UTC)),
        ("2026-10-20T14:30+02:00", False, datetime(2026, 10, 20, 12, 30, tzinfo=UTC)),
        ("12h", False, datetime(2026, 10, 26, 3, 30, tzinfo=UTC)),
        ("7d", True, datetime(2026, 10, 19, 15, 30, tzinfo=UTC)),
        ("2w", False, datetime(2026, 10, 12, 15, 30, tzinfo=UTC)),
    ],
)
def test_window_bounds(text: str, end: bool, expected: datetime) -> None:
    assert parse_bound(text, NOW, CHICAGO, end) == expected


@pytest.mark.parametrize(
    "text", ["10/19/2026", "yesterday", "7", "3m", "", "2026-13-45", "2026-10-19T25:00"]
)
def test_window_bound_rejects_other_text(text: str) -> None:
    with pytest.raises(ValueError, match="a date, a time or an age such as"):
        parse_bound(text, NOW, CHICAGO, False)


def test_window_holds_exclusive_ts_bounds() -> None:
    window = Window.parse("2026-10-20", "2026-10-20", NOW, CHICAGO)
    start = datetime(2026, 10, 20, tzinfo=CHICAGO).timestamp()
    assert window.since == f"{start:.6f}"
    assert window.until == f"{start + 86400:.6f}"
    assert window.contains(f"{start + 60:.6f}")
    assert not window.contains(f"{start:.6f}")
    assert not window.contains(f"{start + 86400:.6f}")
    assert Window().contains("1.000001")


def test_window_rejects_since_after_until() -> None:
    with pytest.raises(ValueError, match="--since must be before --until"):
        Window.parse("2026-10-21", "2026-10-20T12:00", NOW, CHICAGO)


def test_window_names_the_flag_it_cannot_read() -> None:
    with pytest.raises(ValueError, match=r"^--until must be"):
        Window.parse(None, "soon", NOW, CHICAGO)
