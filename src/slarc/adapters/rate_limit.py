# ABOUTME: Per-method token buckets that pace Slack Web API calls to each method's rate tier.
# ABOUTME: Every call, including each page of a paginated method, acquires a token first.
import time
from collections.abc import Callable

TIER_RATES = {2: 20, 3: 50, 4: 100}
METHOD_TIERS = {
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
}


class TokenBucket:
    """One bucket per method, each holding a single token that refills at rate_per_minute."""

    def __init__(
        self,
        rate_per_minute: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_second = rate_per_minute / 60
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, tuple[float, float]] = {}

    def acquire(self, method: str) -> float:
        """Takes a token for method, sleeping until one is available; returns seconds waited."""
        now = self._clock()
        tokens, last = self._buckets.get(method, (1.0, now))
        tokens = min(1.0, tokens + (now - last) * self._per_second)
        waited = 0.0
        if tokens < 1.0:
            waited = (1.0 - tokens) / self._per_second
            self._sleep(waited)
            now += waited
            tokens = 1.0
        self._buckets[method] = (tokens - 1.0, now)
        return waited


class TieredLimiter:
    """Routes each method to the token bucket for its Slack rate tier."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._buckets = {tier: TokenBucket(rate, clock, sleep) for tier, rate in TIER_RATES.items()}

    def acquire(self, method: str) -> float:
        return self._buckets[METHOD_TIERS[method]].acquire(method)
