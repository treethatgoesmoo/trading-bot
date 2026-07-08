"""Async token-bucket rate limiter with optional daily budget tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from core.paths import API_BUDGET_FILE

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
    """Wraps an AsyncTokenBucket with a daily call counter.

    The counter persists to .api_budget.json so a mid-day restart doesn't
    reset it to 0 and blow through the provider's real (server-side) budget.
    """

    def __init__(self, bucket: AsyncTokenBucket, daily_limit: int) -> None:
        self.bucket = bucket
        self.daily_limit = daily_limit
        self.daily_used = 0
        self._reset_date = datetime.now(tz=ZoneInfo("America/New_York")).date()
        self._restore()

    def _restore(self) -> None:
        try:
            with open(API_BUDGET_FILE) as f:
                entry = json.load(f).get(self.bucket.name)
            if entry and entry.get("date") == self._reset_date.isoformat():
                self.daily_used = int(entry.get("used", 0))
                logger.info("%s: restored daily budget count (%d used today)",
                            self.bucket.name, self.daily_used)
        except Exception:
            pass

    def _persist(self) -> None:
        try:
            data = {}
            if os.path.exists(API_BUDGET_FILE):
                with open(API_BUDGET_FILE) as f:
                    data = json.load(f)
            data[self.bucket.name] = {"date": self._reset_date.isoformat(), "used": self.daily_used}
            tmp = API_BUDGET_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, API_BUDGET_FILE)
        except Exception:
            pass  # best-effort — never let bookkeeping break a fetch

    async def acquire(self, timeout: float = 10.0) -> bool:
        today = datetime.now(tz=ZoneInfo("America/New_York")).date()
        if today != self._reset_date:
            logger.info("%s: daily counter reset (%d used yesterday)", self.bucket.name, self.daily_used)
            self.daily_used = 0
            self._reset_date = today
            self._persist()

        if self.daily_used >= self.daily_limit:
            logger.warning("%s: daily budget exhausted (%d/%d)", self.bucket.name, self.daily_used, self.daily_limit)
            return False

        # Reserve BEFORE awaiting the bucket: concurrent acquires sitting at
        # limit-1 would otherwise all pass the check during the await and
        # push the count past the limit. Refund if the bucket times out.
        self.daily_used += 1
        self._persist()
        ok = await self.bucket.acquire(timeout)
        if not ok:
            self.daily_used -= 1
            self._persist()
        return ok

    @property
    def remaining_daily(self) -> int:
        return max(0, self.daily_limit - self.daily_used)
