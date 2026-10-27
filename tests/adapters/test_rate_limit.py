# ABOUTME: Tests for the per-method minute window and the Slack tier limiter.
# ABOUTME: A fake monotonic timer advances only when the window sleeps or the test says so.
import pytest
from hypothesis import given
from hypothesis import strategies as st

from slarc.adapters.rate_limit import METHOD_TIERS, TIER_RATES, MinuteWindow, TieredLimiter


class FakeTimer:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_window_allows_a_burst_of_rate_calls_then_waits_for_the_oldest():
    timer = FakeTimer()
    window = MinuteWindow(3, timer.clock, timer.sleep)
    assert [window.acquire("conversations.replies") for _ in range(3)] == [0, 0, 0]
    timer.now += 10
    assert window.acquire("conversations.replies") == pytest.approx(50)
    assert timer.sleeps == [pytest.approx(50)]


def test_window_forgets_calls_older_than_a_minute():
    timer = FakeTimer()
    window = MinuteWindow(2, timer.clock, timer.sleep)
    window.acquire("search.messages")
    window.acquire("search.messages")
    timer.now += 60
    assert window.acquire("search.messages") == 0
    assert timer.sleeps == []


def test_window_is_per_method():
    timer = FakeTimer()
    window = MinuteWindow(1, timer.clock, timer.sleep)
    assert window.acquire("conversations.history") == 0
    assert window.acquire("conversations.replies") == 0
    assert timer.sleeps == []


@given(
    rate=st.integers(min_value=1, max_value=20),
    gaps=st.lists(st.floats(min_value=0, max_value=30), min_size=1, max_size=80),
)
def test_window_never_lets_more_than_rate_calls_into_any_minute(
    rate: int, gaps: list[float]
) -> None:
    timer = FakeTimer()
    window = MinuteWindow(rate, timer.clock, timer.sleep)
    calls = []
    for gap in gaps:
        timer.now += gap
        window.acquire("users.list")
        calls.append(timer.now)
    for i, start in enumerate(calls):
        assert sum(1 for t in calls[i:] if t < start + 60) <= rate


def test_tiered_limiter_uses_each_method_tier_rate():
    timer = FakeTimer()
    limiter = TieredLimiter(timer.clock, timer.sleep)
    for method, rate in (("search.messages", 20), ("conversations.history", 50)):
        assert [limiter.acquire(method) for _ in range(rate)] == [0] * rate
        assert limiter.acquire(method) == pytest.approx(60)
        timer.now += 60


def test_tiers_match_slack_documentation():
    assert TIER_RATES == {2: 20, 3: 50, 4: 100}
    assert {m for m, t in METHOD_TIERS.items() if t == 2} == {
        "search.messages",
        "users.list",
        "conversations.list",
        "usergroups.list",
        "emoji.list",
    }
    assert {m for m, t in METHOD_TIERS.items() if t == 3} == {
        "conversations.history",
        "conversations.replies",
        "conversations.info",
    }
    assert {m for m, t in METHOD_TIERS.items() if t == 4} == {
        "files.info",
        "bots.info",
        "auth.test",
        "users.info",
    }
