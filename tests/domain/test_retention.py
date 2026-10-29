# ABOUTME: Tests for retention values, the effective retention of a tracked item and its cutoff.
# ABOUTME: Also covers thread activity, the newest message of each stored message's thread.
from datetime import UTC, datetime, timedelta

import pytest

from slarc.domain.envelope import Envelope, Meta
from slarc.domain.retention import (
    cutoff,
    effective_retention,
    parse_retention,
    retention_source,
    retention_text,
    thread_activity,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(("text", "expected"), [("90d", 90), ("12w", 84), ("forever", "forever")])
def test_parse_retention_reads_days_weeks_and_forever(text: str, expected: object) -> None:
    assert parse_retention(text) == expected


@pytest.mark.parametrize("text", ["12h", "0d", "0w", "90", "ninety", "", "-3d"])
def test_parse_retention_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="--retention"):
        parse_retention(text)


def test_retention_text_writes_days_or_forever() -> None:
    assert retention_text(84) == "84d"
    assert retention_text("forever") == "forever"


def test_an_items_own_retention_wins_over_its_workspaces() -> None:
    assert effective_retention(90, 30) == 90
    assert retention_source(90, 30) == "item"
    assert effective_retention("forever", 30) == "forever"


def test_an_item_without_its_own_retention_follows_its_workspace() -> None:
    assert effective_retention(None, 30) == 30
    assert retention_source(None, 30) == "workspace"


def test_without_any_retention_an_item_keeps_everything() -> None:
    assert effective_retention(None, None) == "forever"
    assert retention_source(None, None) is None


def test_cutoff_counts_back_whole_days_from_now() -> None:
    assert cutoff(30, NOW) == f"{(NOW - timedelta(days=30)).timestamp():.6f}"


def test_forever_has_no_cutoff() -> None:
    assert cutoff("forever", NOW) is None


def _line(ts: str, thread_ts: str | None = None, deleted: bool = False) -> Envelope:
    meta = Meta(kind="deleted" if deleted else "message", channel="C1", ts=ts)
    if deleted:
        return Envelope(meta=meta)
    message = {"ts": ts} | ({"thread_ts": thread_ts} if thread_ts else {})
    return Envelope(meta=meta, object=message)


def test_thread_activity_is_the_newest_message_of_each_thread() -> None:
    versions = {
        "100.000001": [_line("100.000001", "100.000001")],
        "200.000001": [_line("200.000001", "100.000001")],
        "150.000001": [_line("150.000001")],
        "300.000001": [_line("300.000001", "90.000001")],
        "310.000001": [_line("310.000001", "90.000001"), _line("310.000001", deleted=True)],
    }
    assert thread_activity(versions) == {
        "100.000001": "200.000001",
        "200.000001": "200.000001",
        "150.000001": "150.000001",
        "300.000001": "310.000001",
        "310.000001": "310.000001",
    }
