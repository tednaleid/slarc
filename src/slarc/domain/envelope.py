# ABOUTME: Envelope model: meta plus the verbatim Slack object, the shape of each archive row.
# ABOUTME: Also formats fetched_at and run ids.
from datetime import UTC, datetime
from typing import Literal

import msgspec

Kind = Literal[
    "message",
    "deleted",
    "user",
    "emoji",
    "conversation",
    "bot",
    "usergroup",
    "file",
    "image",
    "run",
]


class Meta(msgspec.Struct, kw_only=True, omit_defaults=True):
    """Field order is the encoded key order and follows the spec's meta v1 table."""

    # A factory default keeps schema in every line; a plain default would be omitted.
    schema: int = msgspec.field(default_factory=lambda: 1)
    kind: Kind
    team: str | None = None
    channel: str | None = None
    ts: str | None = None
    id: str | None = None
    source_team: str | None = None
    fetched_at: str | None = None
    run: str | None = None
    api: str | None = None
    slarc: str | None = None
    hash: str | None = None
    plain: str | None = None
    blob: str | None = None


class Envelope(msgspec.Struct, kw_only=True, omit_defaults=True):
    meta: Meta
    object: dict | None = None


def fetched_at(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id(now: datetime, suffix: str = "") -> str:
    stamp = "r_" + now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{suffix}" if suffix else stamp
