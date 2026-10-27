# ABOUTME: Tests for when pull lists a workspace's users, emoji and usergroups, and how it fetches
# ABOUTME: a user a new message references between listings.
import pytest

from slarc.service.pull import PullOptions
from tests.service.pull_harness import BASE, CHANNEL, Harness, message

LISTINGS = ("users.list", "emoji.list", "usergroups.list")
NEW_USER = {
    "id": "U0NEWUSER1",
    "name": "ada",
    "real_name": "Ada Example",
    "profile": {"real_name": "Ada Example", "display_name": "ada"},
}
NEW_TS = "1792500000.000100"


@pytest.fixture
def h() -> Harness:
    return Harness()


def listings(h: Harness) -> list[str]:
    return [str(call[0]) for call in h.gateway.calls if call[0] in LISTINGS]


def test_first_pull_lists_the_directory_and_records_when(h: Harness):
    h.pull(BASE)

    assert sorted(listings(h)) == sorted(LISTINGS)
    [entry] = h.last_run().auth
    assert entry["directory_listed_at"] == "2026-10-19T13:40:11Z"


def test_a_pull_within_a_day_of_the_listing_lists_nothing(h: Harness):
    h.pull(BASE)
    h.gateway.calls.clear()
    h.clock.advance(22 * 3600)

    h.pull()

    assert listings(h) == []
    assert ("conversations.info", CHANNEL) in h.gateway.calls
    assert "directory_listed_at" not in h.last_run().auth[0]


def test_a_pull_a_day_after_the_listing_lists_again(h: Harness):
    h.pull(BASE)
    h.gateway.calls.clear()
    h.clock.advance(23 * 3600)

    h.pull()

    assert sorted(listings(h)) == sorted(LISTINGS)


def test_full_lists_the_directory_within_a_day(h: Harness):
    h.pull(BASE)
    h.gateway.calls.clear()

    h.pull(options=PullOptions(full=True))

    assert sorted(listings(h)) == sorted(LISTINGS)


def test_a_new_message_from_an_unknown_user_fetches_only_that_user(h: Harness):
    h.pull(BASE)
    h.gateway.calls.clear()
    h.gateway.users_list.append(NEW_USER)
    h.gateway.conversations[CHANNEL].append(
        message(NEW_TS, user=NEW_USER["id"], text=f"hello <@{NEW_USER['id']}>")
    )

    h.pull()

    assert [call for call in h.gateway.calls if call[0] == "users.info"] == [
        ("users.info", NEW_USER["id"])
    ]
    [line] = [env for env in h.lines("user") if env.meta.id == NEW_USER["id"]]
    assert line.meta.api == "users.info"
    [stored] = [env for env in h.lines("message") if env.meta.ts == NEW_TS]
    assert stored.meta.plain == "ada: hello @ada"


def test_a_user_slack_does_not_know_is_asked_for_once_and_the_message_is_kept(h: Harness):
    h.pull(BASE)
    h.gateway.calls.clear()
    h.gateway.conversations[CHANNEL].append(
        message(NEW_TS, user="U0GONE0001", text="<@U0GONE0001> <@U0GONE0001>")
    )

    h.pull()

    assert [call for call in h.gateway.calls if call[0] == "users.info"] == [
        ("users.info", "U0GONE0001")
    ]
    assert any(env.meta.ts == NEW_TS for env in h.lines("message"))
