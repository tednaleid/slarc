# ABOUTME: Output formats; write_rows prints query rows as JSON Lines or CSV for scripts and agents.
# ABOUTME: Times become ISO 8601 with their offset and bytes become base64; nothing is truncated.
import base64
import csv
import json
from collections.abc import Iterable, Sequence
from typing import Literal, TextIO

OutputFormat = Literal["table", "jsonl", "csv"]
ExportFormat = Literal["html", "markdown"]
FORMATS: tuple[OutputFormat, ...] = ("table", "jsonl", "csv")


def write_rows(
    stream: TextIO, fmt: OutputFormat, columns: list[str], rows: Iterable[Sequence[object]]
) -> None:
    """Writes rows as one JSON object per line, keyed by column, or as CSV with a header row.
    The table format is the CLI's own."""
    if fmt == "jsonl":
        for row in rows:
            stream.write(json.dumps(dict(zip(columns, row, strict=True)), default=_json_value))
            stream.write("\n")
        return
    if fmt == "csv":
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([_csv_value(value) for value in row] for row in rows)
        return
    raise ValueError(f"write_rows does not write {fmt}.")


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, list | dict):
        return json.dumps(value, default=_json_value)
    if isinstance(value, str | int | float):
        return value
    return _json_value(value)
