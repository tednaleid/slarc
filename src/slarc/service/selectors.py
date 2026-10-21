# ABOUTME: Resolves a conversation selector to its channel id, and any selector to the tracked
# ABOUTME: items it names without Slack; a selector without a link names the only workspace.
from collections.abc import Callable, Iterable

from slarc.domain.links import ConversationSel, Selector, WorkspaceSel
from slarc.domain.tracking import ItemKey, TrackedItem
from slarc.ports import ArchiveFailed, ArchiveStore, SlackGateway, SlarcError


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


def items_named(
    selector: Selector,
    channel_mode: bool,
    tracked: dict[ItemKey, TrackedItem],
    removed: dict[ItemKey, TrackedItem],
    domains: dict[str, str],
    archive: ArchiveStore,
) -> list[TrackedItem]:
    """The tracked or removed items a selector names, resolved without Slack.

    A selector without a link names the only workspace with tracked items, or with removed
    items when nothing is tracked.
    """
    candidates = {**removed, **tracked}
    if not candidates:
        return []
    if selector.domain is None:
        team = only_workspace(tracked or removed)
    else:
        team = domains.get(selector.domain)
    if team is None:
        return []
    in_team = [item for item in candidates.values() if item.team_id == team]
    if isinstance(selector, WorkspaceSel):
        return in_team
    if isinstance(selector, ConversationSel):
        try:
            channel = archived_conversation(selector, team, archive)
        except ArchiveFailed:
            raise
        except SlarcError:
            return []
        wanted = {("conversation", channel, "")}
    elif channel_mode:
        wanted = {("conversation", selector.channel, "")}
    elif selector.thread_ts is not None:
        wanted = {("thread", selector.channel, str(selector.thread_ts))}
    else:
        ts = str(selector.ts)
        wanted = {(kind, selector.channel, ts) for kind in ("thread", "message")}
        if not any(item.key[1:] in wanted for item in in_team):
            wanted = {
                ("thread", selector.channel, _stored_thread_of(team, selector.channel, ts, archive))
            }
    return [item for item in in_team if item.key[1:] in wanted]


def _stored_thread_of(team: str, channel: str, ts: str, archive: ArchiveStore) -> str:
    """The thread a stored message belongs to, as pull resolves a link to a reply."""
    stored = archive.stored_messages(team, channel).get(ts)
    return (stored.thread_ts if stored is not None else None) or ts
