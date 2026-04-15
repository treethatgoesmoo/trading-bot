"""Async token-bucket rate limiter with optional daily budget tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class AsyncTokenBucket:
    """Leaky token-bucket rate limiter for async code."""

    def __init__(self, max_tokens: int, refill_rate: float, name: str) -> None:
        self.max_tokens = max_tokens
        self.tokens = float(max_tokens)
        self.refill_rate = refill_rate
        self.name = name
        self._lock = asyncio.Lock()
        self._last_refill = time.monotonic()

    async def acquire(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                logger.warning("%s: rate-limit timeout after %.1fs", self.name, timeout)
                return False
            await asyncio.sleep(0.1)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self._last_refill = now


class DailyBudgetLimiter:
    """Wraps an AsyncTokenBucket with a daily call counter."""

    def __init__(self, bucket: AsyncTokenBucket, daily_limit: int) -> None:
        self.bucket = bucket
        self.daily_limit = daily_limit
        self.daily_used = 0
        self._reset_date = datetime.now(tz=ZoneInfo("America/New_York")).date()

    async def acquire(self, timeout: float = 10.0) -> bool:
        today = datetime.now(tz=ZoneInfo("America/New_York")).date()
        if today != self._reset_date:
            logger.info("%s: daily counter reset (%d used yesterday)", self.bucket.name, self.daily_used)
            self.daily_used = 0
            self._reset_date = today

        if self.daily_used >= self.daily_limit:
            logger.warning("%s: daily budget exhausted (%d/%d)", self.bucket.name, self.daily_used, self.daily_limit)
            return False

        ok = await self.bucket.acquire(timeout)
        if ok:
            self.daily_used += 1
        return ok

    @property
    def remaining_daily(self) -> int:
        return max(0, self.daily_limit - self.daily_used)
