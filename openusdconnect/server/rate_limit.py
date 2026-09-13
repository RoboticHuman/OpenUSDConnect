"""Token-bucket rate limiter for per-client transaction throttling."""

from __future__ import annotations

import math
import time


def validate_rate_limit_config(rate: float, burst: int) -> None:
    """Require rate and burst to be disabled or enabled together."""
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("txn_rate must be a finite non-negative number")
    if burst < 0:
        raise ValueError("txn_burst must be non-negative")
    if (rate == 0) != (burst == 0):
        raise ValueError(
            "txn_rate and txn_burst must both be zero or both be positive"
        )


class TokenBucket:
    """Simple token bucket for per-client transaction rate limiting."""

    def __init__(self, rate: float, burst: int):
        validate_rate_limit_config(rate, burst)
        if rate == 0:
            raise ValueError("TokenBucket rate and burst must be positive")
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
