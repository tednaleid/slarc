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
    query = SearchFilter(
        text=text,
        in_channel=conversation.channel,
        in_dm_user=conversation.dm_user,
        from_user=_user(from_user),
        mentions=_user(mentions),
        window=window,
        limit=limit + 1 if limit else 0,
    )
    hits = index.search(query)
    if limit and len(hits) > limit:
        return SearchResult(hits[:limit], more=True)
    return SearchResult(hits)


def _user(text: str | None) -> str | None:
    return text.removeprefix("@") if text is not None else None


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
