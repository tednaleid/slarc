# ABOUTME: Resolves a conversation selector (channel id, #name or @user) to a channel id.
# ABOUTME: Also picks the workspace a selector without a link refers to: the archive's only one.
from collections.abc import Callable, Iterable

from slarc.domain.links import ConversationSel
from slarc.domain.tracking import TrackedItem
from slarc.ports import ArchiveStore, SlackGateway, SlarcError


def resolve_conversation(
    selector: ConversationSel, team_id: str, gateway: SlackGateway, archive: ArchiveStore
) -> str:
    """The channel id a selector names: by id, by #name, or the DM with @user."""
    if selector.channel is not None:
        return selector.channel
    if selector.name is not None:
        named = _named(selector.name)
        found = _find_id(archive.directory_objects(team_id, "conversation").values(), named)
        found = found or _find_id(gateway.list_conversations(), named)
        if found is None:
            raise SlarcError(
                f"No conversation named #{selector.name} was found in workspace {team_id}."
            )
        return found
    if selector.dm_user is not None:
        user_id = _resolve_user(selector.dm_user, team_id, gateway, archive)
        found = _find_id(gateway.list_conversations(), _dm_with(user_id))
        if found is None:
            raise SlarcError(f"No DM with @{selector.dm_user} was found in workspace {team_id}.")
        return found
    raise ValueError("The selector names no conversation.")


def archived_conversation(selector: ConversationSel, team_id: str, archive: ArchiveStore) -> str:
    """The channel id a selector names, found in the archive's directory without Slack."""
    if selector.channel is not None:
        return selector.channel
    conversations = archive.directory_objects(team_id, "conversation").values()
    if selector.name is not None:
        found = _find_id(conversations, _named(selector.name))
        if found is None:
            raise SlarcError(
                f"No conversation named #{selector.name} is in the archive for workspace {team_id}."
            )
        return found
    if selector.dm_user is not None:
        users = archive.directory_objects(team_id, "user").values()
        user_id = _find_id(users, _user_handle(selector.dm_user))
        found = _find_id(conversations, _dm_with(user_id)) if user_id else None
        if found is None:
            raise SlarcError(
                f"No DM with @{selector.dm_user} is in the archive for workspace {team_id}."
            )
        return found
    raise ValueError("The selector names no conversation.")


def only_workspace(tracked: dict[tuple[str, str, str, str], TrackedItem]) -> str:
    teams = {team_id for team_id, _, _, _ in tracked}
    if len(teams) == 1:
        return teams.pop()
    held = "no workspace yet" if not teams else "several workspaces"
    raise SlarcError(f"This archive holds {held}, so use a Slack link to name the workspace.")


def _resolve_user(handle: str, team_id: str, gateway: SlackGateway, archive: ArchiveStore) -> str:
    matches = _user_handle(handle)
    found = _find_id(archive.directory_objects(team_id, "user").values(), matches)
    found = found or _find_id(gateway.users(), matches)
    if found is None:
        raise SlarcError(f"No user @{handle} was found in workspace {team_id}.")
    return found


def _named(name: str) -> Callable[[dict], bool]:
    return lambda conversation: conversation.get("name") == name


def _dm_with(user_id: str) -> Callable[[dict], bool]:
    return lambda conversation: (
        bool(conversation.get("is_im")) and conversation.get("user") == user_id
    )


def _user_handle(handle: str) -> Callable[[dict], bool]:
    return lambda user: (
        handle in (user.get("name"), (user.get("profile") or {}).get("display_name"))
    )


def _find_id(objects: Iterable[dict], predicate: Callable[[dict], bool]) -> str | None:
    return next((obj["id"] for obj in objects if predicate(obj)), None)
