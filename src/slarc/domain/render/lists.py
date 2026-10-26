# ABOUTME: nest_lists turns Slack's flat run of rich_text_list containers into nested lists.
# ABOUTME: Each renderer supplies how an item's elements render; the nesting is shared.
from collections.abc import Callable
from dataclasses import dataclass, field

from slarc.domain.render import dict_list


@dataclass
class ListItem[T]:
    content: T
    children: list[RichList[T]] = field(default_factory=list)


@dataclass
class RichList[T]:
    style: str
    indent: int
    offset: int
    items: list[ListItem[T]]


def nest_lists[T](run: list[dict], render: Callable[[list[dict]], T]) -> list[RichList[T]]:
    """Consecutive rich_text_list containers as nested lists, each item's elements rendered by
    render. Slack sends each indent level as its own container, so a deeper one nests under
    the last item of the one before."""
    roots: list[RichList[T]] = []
    open_lists: list[RichList[T]] = []
    for container in run:
        indent = container.get("indent") or 0
        style = container.get("style") or "bullet"
        items = [
            ListItem(render(dict_list(item.get("elements"))))
            for item in dict_list(container.get("elements"))
        ]
        while open_lists and open_lists[-1].indent > indent:
            open_lists.pop()
        if open_lists and open_lists[-1].indent == indent:
            if open_lists[-1].style == style:
                open_lists[-1].items += items
                continue
            open_lists.pop()
        current = RichList(style, indent, container.get("offset") or 0, items)
        if open_lists and open_lists[-1].items:
            open_lists[-1].items[-1].children.append(current)
        else:
            roots.append(current)
        open_lists.append(current)
    return roots
