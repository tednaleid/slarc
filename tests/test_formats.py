# ABOUTME: Tests for write_rows, which prints query rows as JSON Lines or CSV.
# ABOUTME: Covers value conversion, nulls and the header row.
import io
import json
from datetime import UTC, datetime
from decimal import Decimal

from slarc.formats import write_rows


def test_jsonl_writes_one_object_per_row_with_iso_times() -> None:
    stream = io.StringIO()
    at = datetime(2026, 10, 20, 14, 3, tzinfo=UTC)
    rows = [("a", at, Decimal("1.5"), ["x"], None), ("b", None, None, {"k": 1}, b"hi")]
    write_rows(stream, "jsonl", ["name", "at", "n", "items", "raw"], rows)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines == [
        {"name": "a", "at": "2026-10-20T14:03:00+00:00", "n": "1.5", "items": ["x"], "raw": None},
        {"name": "b", "at": None, "n": None, "items": {"k": 1}, "raw": "aGk="},
    ]


def test_csv_writes_header_and_empty_cells_for_null() -> None:
    stream = io.StringIO()
    write_rows(stream, "csv", ["name", "note"], [("a", None), ("b, c", 'say "hi"')])
    assert stream.getvalue().splitlines() == ["name,note", "a,", '"b, c","say ""hi"""']
