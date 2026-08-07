"""Bollinger band touch watcher — 1-min and 5-min streams.

Pings when a closed candle's close touches or crosses the upper or lower
Bollinger band (default 20-period, 2.0 std). Each band is *armed* while price
is inside it and fires once on the touch; it re-arms only after a close back
strictly inside the band, so a trend hugging the band posts one alert, not one
per bar. A per-(timeframe, band) cooldown backstops choppy oscillation.

Data comes straight from yfinance (via asyncio.to_thread — it's synchronous):
1-min bars are fetched once per minute-boundary and the 5-min stream is
resampled from the same frame, so the watcher costs zero Twelve Data /
Finnhub API budget (same tradeoff as core/vix.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import pytz
import yfinance as yf

import config as cfg
from core.indicators import bollinger

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

BB_TIMEFRAMES = ("1", "5")
BB_TF_LABEL = {"1": "1m", "5": "5m"}


@dataclass
class BBAlert:
    ticker: str
    timeframe: str          # "1" | "5"
    band: str               # "upper" | "lower"
    price: float            # closing price of the touching candle
    band_value: float       # the band level that was touched
    middle: float           # middle band (SMA)
    bar_time: str           # ET display string of the touching bar
    message: str


@dataclass
class _TFState:
    armed_upper: bool = True
    armed_lower: bool = True
    primed: bool = False                 # first evaluation sets arm state silently
    last_bar_boundary: object = None     # skip re-evaluation mid-bar


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


async def _fetch_1m(ticker: str) -> pd.DataFrame | None:
    """Two days of 1-min bars so the 5-min BB(20) window is full even right
    after the open (yesterday's bars pad the rolling window)."""
    def _dl():
        df = yf.Ticker(ticker).history(period="2d", interval="1m")
        return None if df is None or df.empty else _normalize(df)
    try:
        return await asyncio.to_thread(_dl)
    except Exception:
        return None


def _resample_5m(df: pd.DataFrame) -> pd.DataFrame:
    out = (df.set_index("timestamp")
             .resample("5min", label="left", closed="left")
             .agg({"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"})
             .dropna(subset=["close"])
             .reset_index())
    return out


class BBWatcher:
    """Per-ticker Bollinger touch watcher across 1m and 5m timeframes."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.period = cfg.BB_PERIOD
        self.num_std = cfg.BB_STD
        self.cooldown_sec = cfg.BB_ALERT_COOLDOWN_MINUTES * 60
        self._state: dict[str, _TFState] = {tf: _TFState() for tf in BB_TIMEFRAMES}
        self._last_alert: dict[tuple, datetime] = {}   # (tf, band) -> time

    def reset(self) -> None:
        self._state = {tf: _TFState() for tf in BB_TIMEFRAMES}
        self._last_alert = {}

    # ── Detector (pure; operates on a closed-candle DataFrame) ──────────

    def evaluate(self, timeframe: str, df: pd.DataFrame) -> list[BBAlert]:
        if df is None or df.empty or len(df) < self.period + 1:
            return []
        closes = df["close"].astype(float).reset_index(drop=True)
        upper, mid, lower = bollinger(closes, self.period, self.num_std)
        cur, up, lo, md = closes.iloc[-1], upper.iloc[-1], lower.iloc[-1], mid.iloc[-1]
        if pd.isna(up) or pd.isna(lo):
            return []

        st = self._state[timeframe]
        touching_upper = cur >= up
        touching_lower = cur <= lo

        if not st.primed:
            # Establish arm state from the current position without alerting —
            # a bot restart mid-hug must not ping on data it never watched move.
            st.primed = True
            st.armed_upper = not touching_upper
            st.armed_lower = not touching_lower
            return []

        alerts: list[BBAlert] = []
        ts = df["timestamp"].iloc[-1]
        bar_time = (pd.to_datetime(ts).tz_convert(ET).strftime("%I:%M %p ET")
                    if getattr(pd.to_datetime(ts), "tzinfo", None) else str(ts))
        label = BB_TF_LABEL[timeframe]

        if touching_upper and st.armed_upper:
            st.armed_upper = False
            alerts.append(BBAlert(
                self.ticker, timeframe, "upper", round(cur, 2), round(up, 2),
                round(md, 2), bar_time,
                f"{label} close ${cur:.2f} touched the upper band (${up:.2f}) — stretched vs the {self.period}-bar mean",
            ))
        elif not touching_upper:
            st.armed_upper = True

        if touching_lower and st.armed_lower:
            st.armed_lower = False
            alerts.append(BBAlert(
                self.ticker, timeframe, "lower", round(cur, 2), round(lo, 2),
                round(md, 2), bar_time,
                f"{label} close ${cur:.2f} touched the lower band (${lo:.2f}) — stretched vs the {self.period}-bar mean",
            ))
        elif not touching_lower:
            st.armed_lower = True

        return alerts

    # ── Orchestration ────────────────────────────────────────────────────

    async def check(self, now: datetime | None = None) -> list[BBAlert]:
        """Fetch 1m bars once per minute boundary, derive 5m, evaluate both."""
        now = now or datetime.now(ET)

        # Which timeframes actually have a new closed bar this poll?
        due: list[str] = []
        boundaries: dict[str, datetime] = {}
        for tf in BB_TIMEFRAMES:
            minutes = int(tf)
            boundary = now.replace(minute=(now.minute // minutes) * minutes,
                                   second=0, microsecond=0)
            st = self._state[tf]
            if st.last_bar_boundary != boundary:
                due.append(tf)
                boundaries[tf] = boundary
        if not due:
            return []

        df1 = await _fetch_1m(self.ticker)
        if df1 is None or df1.empty:
            return []  # boundaries not recorded — retried next poll

        ts = pd.to_datetime(df1["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        df1 = df1.assign(timestamp=ts)

        all_alerts: list[BBAlert] = []
        for tf in due:
            boundary = boundaries[tf]
            # Closed bars only: drop the in-progress bar so a transient tick
            # can't false-fire (same rationale as the RSI watcher).
            df = df1[(df1["timestamp"].dt.tz_convert(ET) < boundary).values]
            if tf == "5":
                df = _resample_5m(df)
            self._state[tf].last_bar_boundary = boundary
            for alert in self.evaluate(tf, df):
                key = (alert.timeframe, alert.band)
                last = self._last_alert.get(key)
                if last is not None and (now - last).total_seconds() < self.cooldown_sec:
                    continue
                self._last_alert[key] = now
                all_alerts.append(alert)
        return all_alerts
