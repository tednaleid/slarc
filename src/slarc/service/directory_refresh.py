# ABOUTME: Refreshes a workspace's directory (users, emoji, usergroups, bots, conversations),
# ABOUTME: listing it at most daily; appends a line only when an object changed.
from collections.abc import Iterable
from datetime import datetime, timedelta

from slarc.domain.directory import DirectoryView
from slarc.domain.envelope import Kind
from slarc.ports import ArchiveStore, AuthFailed, NotVisible, SlackGateway, SlarcError
from slarc.service.appends import append_directory
from slarc.unit_of_work import UnitOfWork

BOTS_API = "bots.info"
USERS_API = "users.info"
LISTING_INTERVAL = timedelta(days=1)


def listing_due(uow: UnitOfWork, team_id: str) -> bool:
    """Whether the workspace's users, emoji and usergroups were last listed a day or more ago,
    or never."""
    listed = uow.archive.directory_listed_at(team_id)
    return listed is None or uow.clock.now() - datetime.fromisoformat(listed) >= LISTING_INTERVAL


def refresh_directory(
    gateway: SlackGateway,
    team_id: str,
    channels: set[str],
    uow: UnitOfWork,
    bot_ids: Iterable[str] = (),
    *,
    listing: bool = True,
) -> DirectoryView:
    """Appends changed directory lines and returns the directory with the objects just read.

    Users, emoji and usergroups are listed only when listing is true; otherwise the view holds
    the archive's. Bots are fetched only for the given ids that the archive does not hold yet.
    """
    archive = uow.archive
    users, emoji, usergroups = {}, {}, {}
    if listing:
        users = {user["id"]: user for user in gateway.users()}
        emoji = {name: {"name": name, "url": url} for name, url in gateway.emoji().items()}
        usergroups = {group["id"]: group for group in gateway.usergroups()}
    bots = unknown_bots(gateway, team_id, bot_ids, archive)
    conversations = {}
    for channel in sorted(channels):
        try:
            conversations[channel] = gateway.conversation_info(channel)
        except NotVisible:
            continue

    fresh: list[tuple[Kind, str, dict[str, dict]]] = [
        ("user", "users.list", users),
        ("emoji", "emoji.list", emoji),
        ("usergroup", "usergroups.list", usergroups),
        ("bot", BOTS_API, bots),
        ("conversation", "conversations.info", conversations),
    ]
    for kind, api, objects in fresh:
        append_directory(uow, team_id, kind, objects, api)

    def current(kind: Kind, objects: dict[str, dict]) -> dict[str, dict]:
        return archive.directory_objects(team_id, kind) | objects

    return DirectoryView(
        users=current("user", users),
        conversations=current("conversation", conversations),
        usergroups=current("usergroup", usergroups),
        emoji={name: obj.get("url", "") for name, obj in current("emoji", emoji).items()},
        bots=current("bot", bots),
    )


def unknown_bots(
    gateway: SlackGateway, team_id: str, bot_ids: Iterable[str], archive: ArchiveStore
) -> dict[str, dict]:
    """The bots.info object of each given bot the archive does not hold yet."""
    wanted = set(bot_ids)
    if not wanted:
        return {}
    bots = {}
    for bot_id in sorted(wanted - archive.directory_state(team_id, "bot").keys()):
        try:
            bots[bot_id] = gateway.bot_info(bot_id)
        except AuthFailed:
            raise
        except SlarcError:
            # A bot Slack no longer knows still renders from the message's bot_profile.
            continue
    return bots


def unknown_users(
    gateway: SlackGateway, user_ids: Iterable[str], missing: set[str]
) -> dict[str, dict]:
    """The users.info object of each given user; a user Slack does not return is added to
    missing instead, so a run asks for it once."""
    users = {}
    for user_id in sorted(set(user_ids) - missing):
        try:
            users[user_id] = gateway.user_info(user_id)
        except AuthFailed:
            raise
        except SlarcError:
            # A message from a user Slack no longer returns still renders with the raw id.
            missing.add(user_id)
    return users
