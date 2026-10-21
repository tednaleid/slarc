# ABOUTME: Retries a rate-limited Slack call after the wait Slack asks for, counting the wait
# ABOUTME: down a second at a time so the CLI can show it.
import time
from collections.abc import Callable

from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState


class RetryAfterHandler(RateLimitErrorRetryHandler):
    """Retries a rate-limited call after the Retry-After seconds Slack asks for, one second at a
    time, calling waiting with the seconds left before each and with 0 when the wait ends."""

    def __init__(
        self,
        waiting: Callable[[int], None],
        max_retry_count: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(max_retry_count=max_retry_count)
        self.waiting = waiting
        self.sleep = sleep

    def prepare_for_next_attempt(
        self,
        *,
        state: RetryState,
        request: HttpRequest,
        response: HttpResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        state.next_attempt_requested = True
        headers = response.headers if response is not None else {}
        retry_after = next((v[0] for k, v in headers.items() if k.lower() == "retry-after"), "")
        for left in range(int(retry_after) if retry_after.isdigit() else 1, 0, -1):
            self.waiting(left)
            self.sleep(1)
        self.waiting(0)
        state.increment_current_attempt()
