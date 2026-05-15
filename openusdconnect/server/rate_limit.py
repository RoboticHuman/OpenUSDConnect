"""Token-bucket rate limiter for per-client transaction throttling."""

from __future__ import annotations

import time


class TokenBucket:
    """Simple token bucket for per-client transaction rate limiting."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate  # tokens per second
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()

    def try_consume(self) -> float:
        """Try to consume one token.

        Returns 0.0 if a token was consumed, otherwise the number of
        seconds to wait before a token becomes available.
        """
        now = time.monotonic()
        self._tokens = min(
            self.burst,
            self._tokens + (now - self._last) * self.rate,
        )
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self.rate
