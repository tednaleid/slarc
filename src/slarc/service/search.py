# ABOUTME: Search use case: stored messages whose plain text contains a phrase, newest first.
# ABOUTME: Reads --in as #channel-name, a conversation id or link, and --from as a user or id.
from slarc.domain.links import ConversationSel, parse_selector
from slarc.ports import QueryIndex, SearchHit, SlarcError

DEFAULT_LIMIT = 50


def search(
    index: QueryIndex,
    text: str,
    in_conv: str | None,
    from_user: str | None,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    if not text.strip():
        raise SlarcError("Give some text to search for.")
    user = from_user.removeprefix("@") if from_user is not None else None
    return index.search(text, _conversation(in_conv), user, limit)


def _conversation(text: str | None) -> str | None:
    """The conversation id or name --in names."""
    if text is None:
        return None
    unreadable = SlarcError(f"--in takes #channel-name or a conversation id, not '{text}'.")
    try:
        selector = parse_selector(text)
    except ValueError:
        raise unreadable from None
    if not isinstance(selector, ConversationSel):
        raise unreadable
    found = selector.channel or selector.name
    if found is None:
        raise unreadable
    return found
