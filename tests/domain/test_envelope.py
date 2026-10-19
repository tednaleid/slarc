# ABOUTME: Tests for the JSONL envelope model, line encoding, timestamps and run records.
# ABOUTME: Covers meta key order, deleted lines, Slack key order round trips and run envelopes.
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from slarc.domain.envelope import (
    Envelope,
    Meta,
    decode_line,
    encode_line,
    fetched_at,
    month_of,
    run_id,
)
from slarc.domain.tracking import (
    DownloadState,
    RunRecord,
    TrackedItem,
    run_envelope,
    run_from_envelope,
)

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"


def test_meta_is_first_key_and_order_matches_spec():
    meta = Meta(
        kind="message",
        blob="b",
        plain="p",
        hash="h",
        slarc="0.1.0",
        api="conversations.history",
        run="r_20261019T120000Z",
        fetched_at="2026-10-19T12:00:00Z",
        source_team="T1",
        id="i",
        ts="1792410693.663869",
        channel="C1",
        team="T1",
    )
    line = encode_line(Envelope(meta=meta, object={"text": "hi"}))
    assert line.endswith(b"\n")
    assert b" " not in line.replace(b"hi", b"")
    decoded = json.loads(line)
    assert list(decoded) == ["meta", "object"]
    assert list(decoded["meta"]) == [
        "schema",
        "kind",
        "team",
        "channel",
        "ts",
        "id",
        "source_team",
        "fetched_at",
        "run",
        "api",
        "slarc",
        "hash",
        "plain",
        "blob",
    ]
    assert decoded["meta"]["schema"] == 1


def test_unset_meta_fields_are_omitted():
    line = encode_line(Envelope(meta=Meta(kind="user", team="T1", id="U1")))
    assert list(json.loads(line)["meta"]) == ["schema", "kind", "team", "id"]


def test_deleted_line_has_no_object():
    meta = Meta(kind="deleted", team="T1", channel="C1", ts="1792410693.663869")
    decoded = json.loads(encode_line(Envelope(meta=meta)))
    assert list(decoded) == ["meta"]
    assert decode_line(encode_line(Envelope(meta=meta))).object is None


def test_round_trip_preserves_slack_key_order():
    messages = json.loads((SPIKE / "conversations.replies.json").read_text())["messages"]
    for message in messages:
        env = Envelope(meta=Meta(kind="message", team="T1"), object=message)
        back = decode_line(encode_line(env))
        assert back.object is not None
        assert back.object == message
        assert list(back.object) == list(message)
        assert back == env


def test_unknown_meta_keys_are_ignored():
    line = b'{"meta":{"schema":1,"kind":"user","future":"x"},"object":{"b":1,"a":2}}\n'
    env = decode_line(line)
    assert env.meta.kind == "user"
    assert env.object is not None
    assert list(env.object) == ["b", "a"]


def test_non_ascii_is_written_as_utf8():
    line = encode_line(Envelope(meta=Meta(kind="user"), object={"name": "café"}))
    assert "café".encode() in line


def test_month_of_uses_utc():
    assert month_of("1792410693.663869") == "2026-10"
    assert month_of("1793491199.000000") == "2026-10"
    assert month_of("1793491200.000000") == "2026-11"


def test_fetched_at_is_utc_seconds():
    now = datetime(2026, 10, 19, 12, 30, 45, 999999, tzinfo=timezone(timedelta(hours=-5)))
    assert fetched_at(now) == "2026-10-19T17:30:45Z"


def test_run_id_format():
    now = datetime(2026, 10, 19, 12, 30, 45, 123, tzinfo=UTC)
    assert run_id(now) == "r_20261019T123045Z"


def make_run() -> RunRecord:
    return RunRecord(
        run="r_20261019T123045Z",
        command="pull",
        args=["--since", "2026-01-01"],
        started="2026-10-19T12:30:45Z",
        finished="2026-10-19T12:31:00Z",
        teams=["T1"],
        auth=[{"team_id": "T1", "domain": "naleidworkspace", "source": "desktop"}],
        counts={
            "messages_seen": 12,
            "rate_limit_wait_s": 1.5,
            "api_calls": {"auth.test": 1, "conversations.replies": 2},
            "threads_read": {"history": 2},
        },
        errors=[{"channel": "C9", "error": "not_visible"}],
        tracked=[
            TrackedItem(
                team_id="T1",
                kind="conversation",
                channel="C1",
                ts=None,
                since="2026-01-01",
                until=None,
                newest_ts="1792410693.663869",
                oldest_ts="1790000000.000000",
                backfill_complete=False,
                last_search_at=None,
                last_pull_at="2026-10-19T12:31:00Z",
                status="ok",
                last_read={"U1": "1792410693.663869"},
            )
        ],
        downloads=[
            DownloadState(
                team_id="T1",
                target_kind="file",
                target="F1",
                url="https://files.slack.com/x",
                status="pending",
                attempts=1,
                last_error=None,
            )
        ],
    )


def test_run_record_round_trip():
    run = make_run()
    env = run_envelope(run, "0.1.0")
    assert env.meta.kind == "run"
    assert env.meta.team is None
    assert env.meta.hash is None
    assert env.meta.run == run.run
    assert env.meta.slarc == "0.1.0"
    assert env.meta.fetched_at == run.finished
    assert env.object is not None
    assert env.object["command"] == "pull"
    assert run_from_envelope(decode_line(encode_line(env))) == run


def test_run_envelope_before_finish_uses_started():
    run = make_run()
    run.finished = None
    assert run_envelope(run, "0.1.0").meta.fetched_at == run.started


def test_tracked_item_key():
    item = make_run().tracked[0]
    assert item.key == ("T1", "conversation", "C1", "")
