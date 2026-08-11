"""Candlestick pattern watcher — 1-hour and 1-day timeframes.

Logs (and pings) when the last COMPLETED 1-hour or daily candle forms a named
candlestick pattern, using core.indicators.detect_candlestick_patterns — the
same detectors backtest_patterns.py replays. Observational only: daily and
weekly patterns backtested to no standalone directional edge
(docs/strategy-log.md), so posts are context, not trade signals; the 1-hour
horizon has no backtest either way. Every hit is appended to
data/candle_patterns-*.jsonl so a 1h backtest has real capture to grade later.

Completed-candle discipline mirrors the backtest: the daily read uses candles
strictly before today (what a morning read of yesterday's candle can know),
and the hourly read only evaluates bars whose full 60 minutes have elapsed.
The 15:30–16:00 half-hour stub is deliberately never evaluated — it isn't a
1-hour candle.

Data comes straight from yfinance (via asyncio.to_thread — it's synchronous):
one 1h fetch per hourly boundary and one daily fetch per day, so the watcher
costs zero Twelve Data / Finnhub API budget (same tradeoff as bb_watcher.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

from core.indicators import detect_candlestick_patterns

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

CANDLE_TIMEFRAMES = ("60", "D")
CANDLE_TF_LABEL = {"60": "1h", "D": "1D"}
DETECT_TAIL = 30  # candles handed to the detector (same as backtest_patterns)

# Hourly bars are anchored to the 9:30 open (9:30, 10:30, ... 14:30 starts).
_SESSION_OPEN_HOUR, _SESSION_OPEN_MINUTE = 9, 30


@dataclass
class CandleAlert:
    ticker: str
    timeframe: str          # "60" | "D"
    name: str               # pattern name, e.g. "Hammer"
    direction: str | None   # "BULLISH" / "BEARISH" / None (indecision)
    candles: int            # bars forming the pattern
    close: float            # close of the completing candle
    bar_time: str           # ET display string of the completing candle
    candle_ts: str          # UTC ISO of the completing candle's bar start
    message: str


@dataclass
class _State:
    last_hour_boundary: datetime | None = None
    last_daily_date: object = None


def _normalize(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    df["timestamp"] = ts
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


async def _fetch(ticker: str, period: str, interval: str) -> pd.DataFrame | None:
    def _dl():
        return _normalize(yf.Ticker(ticker).history(period=period, interval=interval))
    try:
        return await asyncio.to_thread(_dl)
    except Exception:
        return None


class CandleWatcher:
    """Per-ticker candlestick pattern watcher on 1h and 1D completed candles."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._state = _State()
        # (timeframe, candle_ts, name) triples already logged — seeded from
        # today's candle_patterns file so a restart can't re-post a pattern.
        self._logged: set[tuple[str, str, str]] = set()
        self.seeded = False

    def reset(self) -> None:
        self._state = _State()
        self._logged = set()
        self.seeded = False

    def seed(self, entries: list[dict]) -> None:
        """Mark already-captured patterns (today's jsonl rows) as logged."""
        for e in entries:
            if e.get("ticker") == self.ticker:
                self._logged.add((e.get("timeframe"), e.get("candle_ts"), e.get("name")))
        self.seeded = True

    # ── Detector (pure; operates on a completed-candle DataFrame) ────────

    def evaluate(self, timeframe: str, df: pd.DataFrame) -> list[CandleAlert]:
        if df is None or df.empty:
            return []
        hits = detect_candlestick_patterns(df.tail(DETECT_TAIL))
        if not hits:
            return []
        last = df.iloc[-1]
        ts = pd.to_datetime(last["timestamp"])
        candle_ts = ts.tz_convert("UTC").isoformat()
        ts_et = ts.tz_convert(ET)
        bar_time = (ts_et.strftime("%b %d") if timeframe == "D"
                    else ts_et.strftime("%I:%M %p ET %b %d"))
        label = CANDLE_TF_LABEL[timeframe]
        close = round(float(last["close"]), 2)

        alerts: list[CandleAlert] = []
        for h in hits:
            key = (timeframe, candle_ts, h["name"])
            if key in self._logged:
                continue
            self._logged.add(key)
            side = h["direction"] or "indecision"
            alerts.append(CandleAlert(
                self.ticker, timeframe, h["name"], h["direction"], h["candles"],
                close, bar_time, candle_ts,
                f"{h['name']} ({side}, {h['candles']}-candle) on the {label} chart — "
                f"candle closed ${close:.2f}",
            ))
        return alerts

    # ── Orchestration ────────────────────────────────────────────────────

    async def check(self, now: datetime | None = None) -> list[CandleAlert]:
        """Evaluate 1D once per day and 1h once per hourly bar close."""
        now = now or datetime.now(ET)
        alerts: list[CandleAlert] = []

        # Daily: candles strictly before today — the same information set as
        # backtest_patterns' 9:35 read of yesterday's completed candle.
        if self._state.last_daily_date != now.date():
            daily = await _fetch(self.ticker, "3mo", "1d")
            if daily is not None:
                daily = daily[daily["timestamp"].dt.tz_convert(ET).dt.date < now.date()]
                self._state.last_daily_date = now.date()
                alerts.extend(self.evaluate("D", daily))

        # Hourly: latest boundary of the form 10:30, 11:30, ... 15:30 ET at
        # which a full 60-minute bar has just closed.
        session_open = now.replace(hour=_SESSION_OPEN_HOUR, minute=_SESSION_OPEN_MINUTE,
                                   second=0, microsecond=0)
        hours_done = int((now - session_open).total_seconds() // 3600)
        if hours_done >= 1:
            boundary = session_open + timedelta(hours=min(hours_done, 6))
            if self._state.last_hour_boundary != boundary:
                hourly = await _fetch(self.ticker, "5d", "1h")
                if hourly is not None:
                    # Completed bars only: the bar's full hour must have elapsed.
                    cutoff = pd.Timestamp(now) - pd.Timedelta(hours=1)
                    hourly = hourly[hourly["timestamp"].dt.tz_convert(ET) <= cutoff]
                    self._state.last_hour_boundary = boundary
                    alerts.extend(self.evaluate("60", hourly))

        return alerts
