# ABOUTME: Shared harness for pull tests: spike fixtures, a fake gateway and an in-memory archive.
# ABOUTME: Harness.pull runs one pull in its own unit of work and advances the clock an hour.
import json
from pathlib import Path

from slarc import __version__
from slarc.adapters.slack_gateway import MESSAGE_PAGE_LIMIT
from slarc.domain.envelope import Envelope, Kind
from slarc.domain.links import parse_selector
from slarc.domain.tracking import RunRecord, TrackedItem, run_from_envelope
from slarc.ports import Session
from slarc.service.pull import PullOptions, PullReport, pull
from slarc.unit_of_work import UnitOfWork
from tests.fakes import (
    SPIKE_AUTH,
    FakeClock,
    FakeConnector,
    FakeDownloader,
    FakeSlackGateway,
    InMemoryArchiveStore,
    InMemoryBlobStore,
    RecordingReporter,
)

SPIKE = Path(__file__).parent.parent / "fixtures" / "spike"
TEAM = "T0CB5LYPVFA"
CHANNEL = "C0CA5396G03"
ME = "U0CA538UEN7"
PARENT_TS = "1792410693.663869"
REPLY_TS = "1792410743.787639"
THREAD_TS = [PARENT_TS, REPLY_TS, "1792411464.844209", "1792422623.464629"]
BASE = "https://naleidworkspace.slack.com/archives/C0CA5396G03"
OCT_17 = 1792195200
NALEID = Session(TEAM, "naleidworkspace", "naleid", "desktop", "xoxc-1", "xoxd-1")
SPIKE_FILES = ("F0C9XQFTVRD", "F0CA7TKM285", "F0CB7HWPUTA")
TOMBSTONE_FILE = "F0CAF2TQHQ9"
OTHER_TEAM = "T0OTHER0001"
OTHER_CHANNEL = "C0OTHER0001"
OTHER_LINK = f"https://other.slack.com/archives/{OTHER_CHANNEL}"
OTHER = Session(OTHER_TEAM, "other", "other", "desktop", "xoxc-2", "xoxd-2")


def spike(name: str) -> dict:
    return json.loads((SPIKE / f"{name}.json").read_text())


def channel_messages() -> list[dict]:
    replies = spike("conversations.replies")["messages"]
    in_thread = {m["ts"] for m in replies}
    history = [m for m in spike("conversations.history")["messages"] if m["ts"] not in in_thread]
    return replies + history


def ts_on(day: int, hour: int) -> str:
    """A ts at the given hour of the given day of October 2026, UTC."""
    return f"{OCT_17 + (day - 17) * 86400 + hour * 3600}.000100"


def message(ts: str, **extra: object) -> dict:
    return {"type": "message", "user": ME, "text": f"message at {ts}", "ts": ts, **extra}


def other_gateway() -> FakeSlackGateway:
    """A second workspace with one conversation holding one message."""
    auth = {
        **SPIKE_AUTH,
        "url": "https://other.slack.com/",
        "team": "other",
        "team_id": OTHER_TEAM,
        "user_id": "U0OTHER0001",
    }
    return FakeSlackGateway({OTHER_CHANNEL: [message(ts_on(18, 9))]}, auth=auth)


class CountingConnector(FakeConnector):
    def __init__(self, gateways: dict[Session, FakeSlackGateway]) -> None:
        super().__init__(gateways)
        self.connects: list[str] = []

    def connect(self, workspace: str) -> FakeSlackGateway:
        self.connects.append(workspace)
        return super().connect(workspace)


class Harness:
    def __init__(self) -> None:
        self.archive = InMemoryArchiveStore()
        self.clock = FakeClock()
        self.blobs = InMemoryBlobStore()
        self.downloader = FakeDownloader(self.blobs)
        self.gateway = FakeSlackGateway(
            {CHANNEL: channel_messages()},
            infos={CHANNEL: spike("conversations.info")["channel"]},
            users=spike("users.list")["members"],
            emoji=spike("emoji.list")["emoji"],
            usergroups=spike("usergroups.list")["usergroups"],
            bots={"B0CA9EBAH1C": spike("bots.info.B0CA9EBAH1C")["bot"]},
            files={file_id: spike(f"files.info.{file_id}")["file"] for file_id in SPIKE_FILES},
            deleted_files=[TOMBSTONE_FILE],
        )
        self.connector = CountingConnector({NALEID: self.gateway})
        self.reporter = RecordingReporter()

    def pull(self, *links: str, options: PullOptions | None = None) -> PullReport:
        uow = UnitOfWork(self.archive, self.blobs, self.clock, "pull", list(links), __version__)
        try:
            with uow:
                return pull(
                    [parse_selector(link) for link in links],
                    options or PullOptions(),
                    uow,
                    self.connector,
                    self.downloader,
                    self.reporter,
                )
        finally:
            self.clock.advance(3600)

    def lines(self, kind: Kind) -> list[Envelope]:
        return [env for env in self.archive.lines if env.meta.kind == kind]

    def last_run(self) -> RunRecord:
        return run_from_envelope(self.lines("run")[-1])

    def tracked(self) -> list[TrackedItem]:
        return list(self.archive.tracked_items().values())

    def change_message(self, ts: str, **changes: object) -> None:
        messages = self.gateway.conversations[CHANNEL]
        self.gateway.conversations[CHANNEL] = [
            {**m, **changes} if m["ts"] == ts else m for m in messages
        ]

    def serve(self, stamps: list[str], page_size: int = MESSAGE_PAGE_LIMIT) -> None:
        self.gateway.conversations[CHANNEL] = [message(ts) for ts in stamps]
        self.gateway.page_size = page_size

    def set_archived(self, archived: bool) -> None:
        self.gateway.infos[CHANNEL] = {**self.gateway.infos[CHANNEL], "is_archived": archived}

    def remove_messages(self, *ts: str) -> None:
        messages = self.gateway.conversations[CHANNEL]
        self.gateway.conversations[CHANNEL] = [m for m in messages if m["ts"] not in ts]
