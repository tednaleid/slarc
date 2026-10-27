# ABOUTME: Per-method minute windows that keep Slack Web API calls within each method's tier.
# ABOUTME: Every call, including each page of a paginated method, passes through its window.
import time
from collections import deque
from collections.abc import Callable

TIER_RATES = {1: 1, 2: 20, 3: 50, 4: 100}
# A minute plus a second of margin, so Slack never sees more than a tier's rate in its minute.
WINDOW_S = 61.0
METHOD_TIERS = {
    "rtm.connect": 1,
    "search.messages": 2,
    "users.list": 2,
    "conversations.list": 2,
    "usergroups.list": 2,
    "emoji.list": 2,
    "conversations.history": 3,
    "conversations.replies": 3,
    "conversations.info": 3,
    "files.info": 4,
    "bots.info": 4,
    "auth.test": 4,
    "users.info": 4,
}


class MinuteWindow:
    """Lets each method make at most rate calls in any 60 seconds, with no wait for a burst
    that stays within that count."""

    def __init__(
        self,
        rate: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = rate
        self._clock = clock
        self._sleep = sleep
        self._calls: dict[str, deque[float]] = {}

    def acquire(self, method: str) -> float:
        """Records a call to method, sleeping first until the window has room; returns seconds
        waited."""
        calls = self._calls.setdefault(method, deque())
        now = self._clock()
        while calls and calls[0] <= now - WINDOW_S:
            calls.popleft()
        waited = 0.0
        if len(calls) >= self._rate:
            waited = calls.popleft() + WINDOW_S - now
            self._sleep(waited)
            # A sleep can run long; the window counts from when the call really goes out.
            now = max(self._clock(), now + waited)
        calls.append(now)
        return waited


class TieredLimiter:
    """Routes each method to the minute window for its Slack rate tier."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._windows = {
            tier: MinuteWindow(rate, clock, sleep) for tier, rate in TIER_RATES.items()
        }

    def acquire(self, method: str) -> float:
        return self._windows[METHOD_TIERS[method]].acquire(method)


_workspace_limiters: dict[str, TieredLimiter] = {}


def workspace_limiter(workspace: str) -> TieredLimiter:
    """The one limiter this process uses for a workspace, so every gateway to it shares its
    windows; a fresh window per gateway would let a new one burst a whole minute's rate."""
    if workspace not in _workspace_limiters:
        _workspace_limiters[workspace] = TieredLimiter()
    return _workspace_limiters[workspace]
