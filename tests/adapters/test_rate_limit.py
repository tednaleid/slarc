# ABOUTME: Tests for the per-method token bucket and the Slack tier limiter.
# ABOUTME: A fake monotonic timer advances only when the bucket sleeps or the test says so.
import pytest

from slarc.adapters.rate_limit import METHOD_TIERS, TIER_RATES, TieredLimiter, TokenBucket


class FakeTimer:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_bucket_waits_when_empty():
    timer = FakeTimer()
    bucket = TokenBucket(50, timer.clock, timer.sleep)
    assert bucket.acquire("conversations.history") == 0
    assert bucket.acquire("conversations.history") == pytest.approx(1.2)
    assert timer.sleeps == [pytest.approx(1.2)]
    timer.now += 0.5
    assert bucket.acquire("conversations.history") == pytest.approx(0.7)


def test_bucket_refills_with_time():
    timer = FakeTimer()
    bucket = TokenBucket(20, timer.clock, timer.sleep)
    bucket.acquire("search.messages")
    timer.now += 3
    assert bucket.acquire("search.messages") == 0
    assert timer.sleeps == []


def test_bucket_is_per_method():
    timer = FakeTimer()
    bucket = TokenBucket(50, timer.clock, timer.sleep)
    assert bucket.acquire("conversations.history") == 0
    assert bucket.acquire("conversations.replies") == 0
    assert timer.sleeps == []


def test_tiered_limiter_uses_each_method_tier_rate():
    timer = FakeTimer()
    limiter = TieredLimiter(timer.clock, timer.sleep)
    waits = {}
    for method in ("search.messages", "conversations.history", "auth.test"):
        limiter.acquire(method)
        waits[method] = limiter.acquire(method)
    assert waits == {
        "search.messages": pytest.approx(3.0),
        "conversations.history": pytest.approx(1.2),
        "auth.test": pytest.approx(0.6),
    }


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
    }
