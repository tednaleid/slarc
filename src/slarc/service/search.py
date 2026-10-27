# ABOUTME: Search use case: stored messages matching text and filters, newest first.
# ABOUTME: Reads --in as #channel-name, @user or a conversation id, and users as a name, id or me.
from dataclasses import dataclass

from slarc.domain.links import ConversationSel, parse_selector
from slarc.domain.window import OPEN_WINDOW, Window
from slarc.ports import SEARCH_LIMIT, QueryIndex, SearchFilter, SearchHit, SlarcError


@dataclass(frozen=True)
class SearchResult:
    """The hits, and whether the limit left more matches out."""

    hits: list[SearchHit]
    more: bool = False


@dataclass(frozen=True)
class _Conversation:
    channel: str | None = None
    dm_user: str | None = None


def search(
    index: QueryIndex,
    text: str | None,
    in_conv: str | None,
    from_user: str | None,
    *,
    mentions: str | None = None,
    window: Window = OPEN_WINDOW,
    limit: int = SEARCH_LIMIT,
) -> SearchResult:
    """Matches of text, if given, narrowed by every filter given; limit 0 means no limit."""
    text = text if text and text.strip() else None
    if text is None and not (in_conv or from_user or mentions or window.since or window.until):
        raise SlarcError(
            "Give text to search for, or a filter such as --in, --from, --mentions or --since."
        )
    conversation = _conversation(in_conv)
    if in_conv is not None and not index.knows_conversation(
        conversation.channel, conversation.dm_user
    ):
        if conversation.dm_user:
            raise SlarcError(f"No DM with @{conversation.dm_user} in the archive. See slarc list.")
        raise SlarcError(f"No conversation named {in_conv} in the archive. See slarc list.")
    users = [_known_user(index, user) for user in (from_user, mentions)]
    query = SearchFilter(
        text=text,
        in_channel=conversation.channel,
        in_dm_user=conversation.dm_user,
        from_user=users[0],
        mentions=users[1],
        window=window,
        limit=limit + 1 if limit else 0,
    )
    hits = index.search(query)
    if limit and len(hits) > limit:
        return SearchResult(hits[:limit], more=True)
    return SearchResult(hits)


def _known_user(index: QueryIndex, text: str | None) -> str | None:
    if text is None:
        return None
    user = text.removeprefix("@")
    if not index.knows_user(user):
        raise SlarcError(f"No user named {user} in the archive.")
    return user


def _conversation(text: str | None) -> _Conversation:
    """The conversation id or name --in names, or the user a DM is with."""
    if text is None:
        return _Conversation()
    unreadable = SlarcError(f"--in takes #channel-name, @user or a conversation id, not '{text}'.")
    try:
        selector = parse_selector(text)
    except ValueError:
        raise unreadable from None
    if not isinstance(selector, ConversationSel):
        raise unreadable
    if selector.dm_user is not None:
        return _Conversation(dm_user=selector.dm_user)
    found = selector.channel or selector.name
    if found is None:
        raise unreadable
    return _Conversation(channel=found)
