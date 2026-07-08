"""Unified async market data client.

Provider priority: Twelve Data → Finnhub → yfinance (free fallback).
Includes retry with exponential backoff and rate limiting.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta

import aiohttp
import pandas as pd
import pytz
import yfinance as yf

import config as cfg
from core import market_calendar
from core.rate_limiter import AsyncTokenBucket, DailyBudgetLimiter

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# ── Retry constants ──────────────────────────────────────────────────────
MAX_RETRIES = 2
BASE_DELAY = 1.0
MAX_DELAY = 8.0
JITTER_FACTOR = 0.5


# ── Exceptions ───────────────────────────────────────────────────────────

class APIError(Exception):
    pass

class RateLimitError(APIError):
    pass

class AllSourcesExhaustedError(APIError):
    pass


# ── Interval mapping: (finnhub_res, twelvedata_interval, yfinance_interval)
INTERVAL_MAP = {
    "1":  ("1",  "1min",  "1m"),
    "5":  ("5",  "5min",  "5m"),
    "15": ("15", "15min", "15m"),
    "30": ("30", "30min", "30m"),
    "D":  ("D",  "1day",  "1d"),
}

YFINANCE_PERIOD = {
    "1":  "1d",
    # 5-min data must cover several sessions: compute_atr needs 15+ regular-hours
    # bars, which "1d" can't provide early in the day.
    "5":  "5d",
    "15": "5d",
    "30": "5d",
    # Daily must cover the swing feature's 300-bar window (SMA200 + 52w high);
    # callers get the tail(count) they asked for either way.
    "D":  "2y",
}


class MarketDataClient:
    """Fetches OHLCV data — Twelve Data (primary) → Finnhub → yfinance (fallback)."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

        # Rate limiters for paid providers
        if cfg.TWELVEDATA_API_KEY:
            self.twelvedata_limiter = DailyBudgetLimiter(
                bucket=AsyncTokenBucket(
                    max_tokens=cfg.TWELVEDATA_CALLS_PER_MIN,
                    refill_rate=cfg.TWELVEDATA_CALLS_PER_MIN / 60.0,
                    name="twelvedata",
                ),
                daily_limit=cfg.TWELVEDATA_DAILY_LIMIT,
            )
        else:
            self.twelvedata_limiter = None

        if cfg.FINNHUB_API_KEY:
            self.finnhub_limiter = AsyncTokenBucket(
                max_tokens=cfg.FINNHUB_CALLS_PER_MIN,
                refill_rate=cfg.FINNHUB_CALLS_PER_MIN / 60.0,
                name="finnhub",
            )
        else:
            self.finnhub_limiter = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Retry logic ──────────────────────────────────────────────────────

    async def _retry(self, coro_factory, provider: str) -> pd.DataFrame:
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RateLimitError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, APIError) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                    jitter = random.uniform(0, JITTER_FACTOR * delay)
                    logger.warning("%s attempt %d/%d failed: %s — retrying in %.1fs",
                                   provider, attempt + 1, MAX_RETRIES + 1, exc, delay + jitter)
                    await asyncio.sleep(delay + jitter)
                else:
                    logger.warning("%s: all %d attempts failed: %s", provider, MAX_RETRIES + 1, exc)
        raise last_exc

    # ── Public API ───────────────────────────────────────────────────────

    async def fetch_candles(self, symbol: str, interval: str = "D", count: int = 100) -> pd.DataFrame:
        """Fetch OHLCV candles. Tries Twelve Data → Finnhub → yfinance.

        Args:
            symbol: Ticker symbol (e.g. "SPY")
            interval: "1", "5", "15", or "D" (daily)
            count: Number of candles to request

        Returns DataFrame with columns: timestamp, open, high, low, close, volume
        """
        # ── 1. Twelve Data (primary) ─────────────────────────────────────
        if self.twelvedata_limiter and cfg.TWELVEDATA_API_KEY:
            if await self.twelvedata_limiter.acquire(timeout=5.0):
                try:
                    return await self._retry(
                        lambda: self._fetch_twelvedata(symbol, interval, count),
                        "TwelveData",
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, APIError) as exc:
                    logger.warning("TwelveData failed for %s: %s", symbol, exc)

        # ── 2. Finnhub ───────────────────────────────────────────────────
        if self.finnhub_limiter and cfg.FINNHUB_API_KEY:
            if await self.finnhub_limiter.acquire(timeout=5.0):
                try:
                    return await self._retry(
                        lambda: self._fetch_finnhub(symbol, interval, count),
                        "Finnhub",
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError, APIError) as exc:
                    logger.warning("Finnhub failed for %s: %s", symbol, exc)

        # ── 3. yfinance (free fallback) ──────────────────────────────────
        try:
            return await self._retry(
                lambda: self._fetch_yfinance(symbol, interval, count),
                "yfinance",
            )
        except Exception as exc:
            # Broad on purpose: whatever the last-resort provider throws, the
            # caller-facing contract is AllSourcesExhaustedError.
            logger.error("yfinance failed for %s: %s", symbol, exc)

        raise AllSourcesExhaustedError(f"All API sources exhausted for {symbol}")

    # ── Twelve Data ──────────────────────────────────────────────────────

    async def _fetch_twelvedata(self, symbol: str, interval: str, count: int) -> pd.DataFrame:
        session = await self._get_session()
        td_interval = INTERVAL_MAP[interval][1]

        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol.upper(),
            "interval": td_interval,
            "outputsize": count,
            "apikey": cfg.TWELVEDATA_API_KEY,
        }

        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                raise RateLimitError("TwelveData rate limited (429)")
            if resp.status != 200:
                raise APIError(f"TwelveData HTTP {resp.status}")
            data = await resp.json()

        # Free-tier credit exhaustion arrives as HTTP 200 with an error body —
        # treat it as a rate limit so we fail over fast instead of retrying.
        if isinstance(data, dict) and str(data.get("code")) == "429":
            raise RateLimitError(f"TwelveData rate limited: {str(data.get('message', ''))[:120]}")

        if "values" not in data:
            msg = data.get("message", "Unknown error")
            raise APIError(f"TwelveData no values for {symbol}: {msg}")

        values = data["values"]
        if not values:
            raise APIError(f"TwelveData empty values for {symbol}")

        df = pd.DataFrame(values)
        df = df.rename(columns={"datetime": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(ET).dt.tz_convert("UTC")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        logger.info("TwelveData: fetched %d candles for %s (%s)", len(df), symbol, td_interval)
        return df.sort_values("timestamp").reset_index(drop=True)

    # ── Finnhub ──────────────────────────────────────────────────────────

    async def _fetch_finnhub(self, symbol: str, interval: str, count: int) -> pd.DataFrame:
        session = await self._get_session()
        resolution = INTERVAL_MAP[interval][0]

        now = int(time.time())
        # The window is wall-clock but candles only print during trading time
        # (~5 sessions per 7 days, ~6.5 of 24 hours intraday) — an unpadded
        # window returns far fewer bars than `count` and silently starves
        # consumers like the swing evaluator's 210-bar minimum.
        if interval == "D":
            from_ts = now - int(count * 86400 * 1.6) - 14 * 86400
        else:
            seconds_per_candle = int(interval) * 60
            from_ts = now - (count * seconds_per_candle * 6)

        url = "https://finnhub.io/api/v1/stock/candle"
        params = {
            "symbol": symbol.upper(),
            "resolution": resolution,
            "from": from_ts,
            "to": now,
            "token": cfg.FINNHUB_API_KEY,
        }

        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                raise RateLimitError("Finnhub rate limited (429)")
            if resp.status != 200:
                raise APIError(f"Finnhub HTTP {resp.status}")
            data = await resp.json()

        if data.get("s") == "no_data":
            raise APIError(f"Finnhub no data for {symbol}")

        required = {"c", "h", "l", "o", "v", "t"}
        if not required.issubset(data.keys()):
            raise APIError(f"Finnhub unexpected response for {symbol}")

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(data["t"], unit="s", utc=True),
            "open": data["o"],
            "high": data["h"],
            "low": data["l"],
            "close": data["c"],
            "volume": data["v"],
        })

        df = df.sort_values("timestamp").reset_index(drop=True)
        if len(df) > count:
            df = df.tail(count).reset_index(drop=True)
        logger.info("Finnhub: fetched %d candles for %s (%s)", len(df), symbol, resolution)
        return df

    # ── yfinance (free fallback) ─────────────────────────────────────────

    async def _fetch_yfinance(self, symbol: str, interval: str, count: int) -> pd.DataFrame:
        yf_interval = INTERVAL_MAP[interval][2]
        period = YFINANCE_PERIOD[interval]

        def _download() -> pd.DataFrame:
            try:
                ticker = yf.Ticker(symbol.upper())
                df = ticker.history(period=period, interval=yf_interval)
            except Exception as exc:  # requests/parse errors → retryable APIError
                raise APIError(f"yfinance error for {symbol}: {exc}") from exc
            if df is None or df.empty:
                raise APIError(f"yfinance no data for {symbol}")
            return df

        df = await asyncio.to_thread(_download)
        df = df.reset_index()

        ts_col = "Datetime" if "Datetime" in df.columns else "Date"
        df = df.rename(columns={
            ts_col: "timestamp",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        timestamps = pd.to_datetime(df["timestamp"])
        if timestamps.dt.tz is not None:
            df["timestamp"] = timestamps.dt.tz_convert("UTC")
        else:
            df["timestamp"] = timestamps.dt.tz_localize(ET).dt.tz_convert("UTC")

        if len(df) > count:
            df = df.tail(count)

        logger.info("yfinance: fetched %d candles for %s (%s)", len(df), symbol, yf_interval)
        return df.sort_values("timestamp").reset_index(drop=True)


# ── Convenience helpers used by strategy.py ──────────────────────────────

def is_market_open() -> bool:
    return market_calendar.is_market_open(datetime.now(ET))
