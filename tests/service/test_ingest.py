# ABOUTME: Tests for MessageWriter, which pull and listen use to append message lines, deletes
# ABOUTME: and the users and bots messages name, counting what they saw and appended.
from slarc import __version__
from slarc.domain.directory import DirectoryView
from slarc.service.held_conversation import HeldConversation
from slarc.service.ingest import MessageWriter
from slarc.unit_of_work import UnitOfWork
from tests.fakes import FakeClock, InMemoryArchiveStore, InMemoryBlobStore
from tests.service.pull_harness import CHANNEL, TEAM, message

TS = "1792500000.000100"


def empty_view() -> DirectoryView:
    return DirectoryView(users={}, conversations={}, usergroups={}, emoji={}, bots={})


def test_writer_appends_a_message_once_and_counts_it() -> None:
    archive = InMemoryArchiveStore()
    seen: list[int] = []
    new: list[int] = []
    with UnitOfWork(archive, InMemoryBlobStore(), FakeClock(), "pull", [], __version__) as uow:
        writer = MessageWriter(
            uow, 100, on_seen=lambda: seen.append(1), on_new=lambda: new.append(1)
        )
        held = HeldConversation(archive, TEAM, CHANNEL)
        for _ in range(2):
            writer.append_messages(held, [message(TS)], "conversations.history", empty_view())

    [line] = [env for env in archive.lines if env.meta.kind == "message"]
    assert (line.meta.ts, line.meta.api) == (TS, "conversations.history")
    assert (len(seen), len(new)) == (1, 1)
