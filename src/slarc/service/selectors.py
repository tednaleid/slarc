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
        name = selector.name

        def named(conversation: dict) -> bool:
            return conversation.get("name") == name

        found = _find_id(archive.directory_objects(team_id, "conversation").values(), named)
        found = found or _find_id(gateway.list_conversations(), named)
        if found is None:
            raise SlarcError(f"No conversation named #{name} was found in workspace {team_id}.")
        return found
    if selector.dm_user is not None:
        user_id = _resolve_user(selector.dm_user, team_id, gateway, archive)

        def dm_with_user(conversation: dict) -> bool:
            return bool(conversation.get("is_im")) and conversation.get("user") == user_id

        found = _find_id(gateway.list_conversations(), dm_with_user)
        if found is None:
            raise SlarcError(f"No DM with @{selector.dm_user} was found in workspace {team_id}.")
        return found
    raise ValueError("The selector names no conversation.")


def only_workspace(tracked: dict[tuple[str, str, str, str], TrackedItem]) -> str:
    teams = {team_id for team_id, _, _, _ in tracked}
    if len(teams) == 1:
        return teams.pop()
    held = "no workspace yet" if not teams else "several workspaces"
    raise SlarcError(f"This archive holds {held}, so use a Slack link to say which to pull from.")


def _resolve_user(handle: str, team_id: str, gateway: SlackGateway, archive: ArchiveStore) -> str:
    def matches(user: dict) -> bool:
        return handle in (user.get("name"), (user.get("profile") or {}).get("display_name"))

    found = _find_id(archive.directory_objects(team_id, "user").values(), matches)
    found = found or _find_id(gateway.users(), matches)
    if found is None:
        raise SlarcError(f"No user @{handle} was found in workspace {team_id}.")
    return found


def _find_id(objects: Iterable[dict], predicate: Callable[[dict], bool]) -> str | None:
    return next((obj["id"] for obj in objects if predicate(obj)), None)
