# ABOUTME: Tests for the envelope helpers that format timestamps and run ids, and for run records.
# ABOUTME: Covers fetched_at, run ids and converting a RunRecord to and from its run envelope.
from datetime import UTC, datetime, timedelta, timezone

import msgspec

from slarc.domain.envelope import Envelope, fetched_at, run_id
from slarc.domain.tracking import (
    DownloadState,
    RunRecord,
    TrackedItem,
    run_envelope,
    run_from_envelope,
)


def test_fetched_at_is_utc_seconds():
    now = datetime(2026, 10, 19, 12, 30, 45, 999999, tzinfo=timezone(timedelta(hours=-5)))
    assert fetched_at(now) == "2026-10-19T17:30:45Z"


def test_run_id_format():
    now = datetime(2026, 10, 19, 12, 30, 45, 123, tzinfo=UTC)
    assert run_id(now) == "r_20261019T123045Z"


def test_run_id_appends_a_suffix():
    now = datetime(2026, 10, 19, 12, 30, 45, tzinfo=UTC)
    assert run_id(now, "3f9a") == "r_20261019T123045Z_3f9a"


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
    assert run_from_envelope(msgspec.json.decode(msgspec.json.encode(env), type=Envelope)) == run


def test_run_envelope_before_finish_uses_started():
    run = make_run()
    run.finished = None
    assert run_envelope(run, "0.1.0").meta.fetched_at == run.started


def test_tracked_item_key():
    item = make_run().tracked[0]
    assert item.key == ("T1", "conversation", "C1", "")
