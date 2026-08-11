"""Candlestick pattern watcher — 1-hour and 1-day timeframes.

Logs (and pings) when the last COMPLETED 1-hour or daily candle forms a named
candlestick pattern, using core.indicators.detect_candlestick_patterns — the
same detectors backtest_patterns.py replays. Observational only: daily and
weekly patterns backtested to no standalone directional edge
(docs/strategy-log.md), so posts are context, not trade signals; the 1-hour
horizon has no backtest either way.

Two data kinds are captured (via the cog, one row per line, UTC ISO times):
- candle_patterns-*.jsonl — every pattern hit, with the full OHLC of the
  candle that completed it.
- candles-*.jsonl — every completed 1h/1D candle itself (ticker, timeframe,
  time = bar start, open/high/low/close/volume), filed under the candle's own
  ET session date. This is the permanent local archive a future 1h backtest
  can grade against — yfinance only serves ~730 days of hourly history, so
  the capture outlives the source.

Completed-candle discipline mirrors the backtest: the daily read uses candles
strictly before today until the session closes (what a morning read of
yesterday's candle can know; after the close today's candle is complete and
included), and the hourly read only evaluates full-hour session bars
(9:30–14:30 ET starts) whose 60 minutes have elapsed. The 15:30–16:00
half-hour stub is deliberately never evaluated or archived — it isn't a
1-hour candle.

Data comes straight from yfinance (via asyncio.to_thread — it's synchronous):
one 1h fetch per hourly boundary and one daily fetch per day, so the watcher
costs zero Twelve Data / Finnhub API budget (same tradeoff as bb_watcher.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytz
import yfinance as yf

from core import market_calendar
from core.indicators import detect_candlestick_patterns

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

CANDLE_TIMEFRAMES = ("60", "D")
CANDLE_TF_LABEL = {"60": "1h", "D": "1D"}
DETECT_TAIL = 30  # candles handed to the detector (same as backtest_patterns)

# Full-hour session bars are anchored to the 9:30 open; the last one starts
# at 14:30 (the 15:30–16:00 stub is not a 1-hour candle).
_HOUR_STARTS = tuple(dtime(h, 30) for h in range(9, 15))
_SESSION_OPEN = dtime(9, 30)


@dataclass
class CandleAlert:
    ticker: str
    timeframe: str          # "60" | "D"
    name: str               # pattern name, e.g. "Hammer"
    direction: str | None   # "BULLISH" / "BEARISH" / None (indecision)
    candles: int            # bars forming the pattern
    open: float             # OHLC of the completing candle
    high: float
    low: float
    close: float
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


def _completed_hourly(df: pd.DataFrame, now: datetime) -> pd.DataFrame | None:
    """Full-hour session bars whose 60 minutes have elapsed."""
    if df is None or df.empty:
        return None
    ts_et = df["timestamp"].dt.tz_convert(ET)
    full_hour = ts_et.dt.time.isin(_HOUR_STARTS)
    elapsed = ts_et <= pd.Timestamp(now) - pd.Timedelta(hours=1)
    return df[(full_hour & elapsed).values]


def _completed_daily(df: pd.DataFrame, now: datetime) -> pd.DataFrame | None:
    """Daily candles strictly before today; today's included once the session
    has closed (13:00 ET on half-days via market_calendar)."""
    if df is None or df.empty:
        return None
    try:
        closed_today = now >= market_calendar.session_close(now)
    except Exception:
        closed_today = now.time() >= dtime(16, 0)
    dates = df["timestamp"].dt.tz_convert(ET).dt.date
    keep = dates <= now.date() if closed_today else dates < now.date()
    return df[keep.values]


def _candle_row(ticker: str, timeframe: str, row) -> dict:
    """One archived candle — the user-facing OHLCV shape, UTC ISO bar start."""
    ts = pd.to_datetime(row["timestamp"])
    return {
        "ticker": ticker, "timeframe": timeframe,
        "time": ts.tz_convert("UTC").isoformat(),
        "open": round(float(row["open"]), 4), "high": round(float(row["high"]), 4),
        "low": round(float(row["low"]), 4), "close": round(float(row["close"]), 4),
        "volume": float(row["volume"]),
        # ET session date the candle belongs to — the capture file it lands in.
        "date": ts.tz_convert(ET).strftime("%Y-%m-%d"),
    }


async def current_read(ticker: str, now: datetime | None = None) -> dict:
    """Stateless on-demand read for !patterns: {timeframe: {hits, candle,
    bar_time} | None}. Same fetches and completed-candle filters as the
    watcher, so the command can never drift from what gets logged."""
    now = now or datetime.now(ET)
    out: dict[str, dict | None] = {}
    for tf, period, interval, filt in (("D", "3mo", "1d", _completed_daily),
                                       ("60", "5d", "1h", _completed_hourly)):
        df = filt(await _fetch(ticker, period, interval), now)
        if df is None or df.empty:
            out[tf] = None
            continue
        last = df.iloc[-1]
        ts_et = pd.to_datetime(last["timestamp"]).tz_convert(ET)
        out[tf] = {
            "hits": detect_candlestick_patterns(df.tail(DETECT_TAIL)),
            "candle": _candle_row(ticker, tf, last),
            "bar_time": (ts_et.strftime("%b %d") if tf == "D"
                         else ts_et.strftime("%I:%M %p ET %b %d")),
        }
    return out


class CandleWatcher:
    """Per-ticker candlestick pattern watcher on 1h and 1D completed candles."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._state = _State()
        # Dedupe state, seeded from the capture files so a restart can't
        # re-post a pattern or re-append a candle.
        self._logged: set[tuple[str, str, str]] = set()    # (tf, candle_ts, name)
        self._archived: set[tuple[str, str]] = set()       # (tf, bar-start ISO)
        self.seeded = False

    def reset(self) -> None:
        self._state = _State()
        self._logged = set()
        self._archived = set()
        self.seeded = False

    def seed(self, pattern_entries: list[dict], candle_entries: list[dict]) -> None:
        """Mark already-captured patterns/candles (jsonl rows) as seen."""
        for e in pattern_entries:
            if e.get("ticker") == self.ticker:
                self._logged.add((e.get("timeframe"), e.get("candle_ts"), e.get("name")))
        for e in candle_entries:
            if e.get("ticker") == self.ticker:
                self._archived.add((e.get("timeframe"), e.get("time")))
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
        o, h, l, c = (round(float(last[k]), 2) for k in ("open", "high", "low", "close"))

        alerts: list[CandleAlert] = []
        for hit in hits:
            key = (timeframe, candle_ts, hit["name"])
            if key in self._logged:
                continue
            self._logged.add(key)
            side = hit["direction"] or "indecision"
            alerts.append(CandleAlert(
                self.ticker, timeframe, hit["name"], hit["direction"], hit["candles"],
                o, h, l, c, bar_time, candle_ts,
                f"{hit['name']} ({side}, {hit['candles']}-candle) on the {label} chart — "
                f"candle closed ${c:.2f}",
            ))
        return alerts

    def _new_candles(self, timeframe: str, df: pd.DataFrame, now: datetime) -> list[dict]:
        """Completed candles not yet archived. Hourly: today's session bars
        (self-healing after a mid-day restart); daily: the last completed bar."""
        if df is None or df.empty:
            return []
        if timeframe == "60":
            todays = df[(df["timestamp"].dt.tz_convert(ET).dt.date == now.date()).values]
            candidates = [row for _, row in todays.iterrows()]
        else:
            candidates = [df.iloc[-1]]
        rows = []
        for row in candidates:
            entry = _candle_row(self.ticker, timeframe, row)
            key = (timeframe, entry["time"])
            if key in self._archived:
                continue
            self._archived.add(key)
            rows.append(entry)
        return rows

    # ── Orchestration ────────────────────────────────────────────────────

    async def check(self, now: datetime | None = None) -> tuple[list[CandleAlert], list[dict]]:
        """Evaluate 1D once per day and 1h once per hourly bar close.

        Returns (pattern alerts, newly completed candles to archive)."""
        now = now or datetime.now(ET)
        alerts: list[CandleAlert] = []
        candles: list[dict] = []

        # Daily: candles strictly before today — the same information set as
        # backtest_patterns' 9:35 read of yesterday's completed candle.
        if self._state.last_daily_date != now.date():
            daily = _completed_daily(await _fetch(self.ticker, "3mo", "1d"), now)
            if daily is not None and not daily.empty:
                self._state.last_daily_date = now.date()
                candles.extend(self._new_candles("D", daily, now))
                alerts.extend(self.evaluate("D", daily))

        # Hourly: latest boundary of the form 10:30, 11:30, ... 15:30 ET at
        # which a full 60-minute bar has just closed.
        session_open = now.replace(hour=_SESSION_OPEN.hour, minute=_SESSION_OPEN.minute,
                                   second=0, microsecond=0)
        hours_done = int((now - session_open).total_seconds() // 3600)
        if hours_done >= 1:
            boundary = session_open + timedelta(hours=min(hours_done, 6))
            if self._state.last_hour_boundary != boundary:
                hourly = _completed_hourly(await _fetch(self.ticker, "5d", "1h"), now)
                if hourly is not None and not hourly.empty:
                    self._state.last_hour_boundary = boundary
                    candles.extend(self._new_candles("60", hourly, now))
                    alerts.extend(self.evaluate("60", hourly))

        return alerts, candles
