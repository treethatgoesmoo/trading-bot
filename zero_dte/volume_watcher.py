"""Volume-spike watcher — 5-min stream.

Pings when a closed 5-min candle's volume clears VOLUME_SPIKE_MULT x its
trailing VOLUME_SPIKE_LOOKBACK-bar average. Armed while volume is under the
threshold and fires once on the spike; it re-arms only after a bar closes
back under the threshold, so a sustained high-volume run posts one alert,
not one per bar. A cooldown backstops choppy oscillation around the
threshold. Mirrors zero_dte/bb_watcher.py's shape/conventions.

Data comes straight from yfinance (via asyncio.to_thread — it's synchronous):
1-min bars are fetched once per minute-boundary and the 5-min stream is
resampled from the same frame, so the watcher costs zero Twelve Data /
Finnhub API budget (same tradeoff as core/vix.py and bb_watcher.py).
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
from zero_dte.strategy import check_relative_volume

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


@dataclass
class VolumeAlert:
    ticker: str
    timeframe: str          # "5"
    direction: str | None   # "BULLISH" | "BEARISH" — implied by the triggering bar's close vs open
    price: float            # closing price of the triggering candle
    ratio: float            # volume / trailing average
    volume: float           # raw volume of the triggering candle
    bar_time: str           # ET display string of the triggering bar
    message: str


@dataclass
class _State:
    armed: bool = True
    primed: bool = False                 # first evaluation sets arm state silently
    last_bar_boundary: object = None     # skip re-evaluation mid-bar


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


async def _fetch_1m(ticker: str) -> pd.DataFrame | None:
    """Two days of 1-min bars so the 5-min rolling-volume window is full even
    right after the open (yesterday's bars pad the lookback)."""
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


class VolumeWatcher:
    """Per-ticker relative-volume spike watcher on the 5m timeframe."""

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.lookback = cfg.VOLUME_SPIKE_LOOKBACK
        self.mult = cfg.VOLUME_SPIKE_MULT
        self.cooldown_sec = cfg.VOLUME_ALERT_COOLDOWN_MINUTES * 60
        self._state = _State()
        self._last_alert: datetime | None = None

    def reset(self) -> None:
        self._state = _State()
        self._last_alert = None

    # ── Detector (pure; operates on a closed-candle DataFrame) ──────────

    def evaluate(self, df: pd.DataFrame) -> list[VolumeAlert]:
        if df is None or df.empty or len(df) < self.lookback + 1:
            return []
        above, ratio = check_relative_volume(df, lookback=self.lookback)
        # check_relative_volume's own threshold (RVOL_THRESHOLD, ~1.2x) is
        # tuned for the entry-filter use case; the spike watcher uses its own,
        # higher bar (VOLUME_SPIKE_MULT, default 2.0x) — recompute the ratio's
        # pass/fail against that instead of reusing `above`.
        spiking = ratio >= self.mult

        st = self._state
        if not st.primed:
            # Establish arm state from the current position without alerting —
            # a bot restart mid-spike must not ping on data it never watched happen.
            st.primed = True
            st.armed = not spiking
            return []

        if not spiking:
            st.armed = True
            return []
        if not st.armed:
            return []

        st.armed = False
        row = df.iloc[-1]
        close = float(row["close"])
        open_ = float(row["open"])
        direction = "BULLISH" if close > open_ else ("BEARISH" if close < open_ else None)
        ts = df["timestamp"].iloc[-1]
        bar_time = (pd.to_datetime(ts).tz_convert(ET).strftime("%I:%M %p ET")
                    if getattr(pd.to_datetime(ts), "tzinfo", None) else str(ts))
        dir_txt = f", closed {direction.lower()}" if direction else ""
        return [VolumeAlert(
            self.ticker, "5", direction, round(close, 2), ratio, float(row["volume"]), bar_time,
            f"5m volume {ratio:.1f}x the {self.lookback}-bar average at ${close:.2f}{dir_txt}",
        )]

    # ── Orchestration ────────────────────────────────────────────────────

    async def check(self, now: datetime | None = None) -> list[VolumeAlert]:
        """Fetch 1m bars once per 5-min bar boundary, derive 5m, evaluate."""
        now = now or datetime.now(ET)

        minutes = 5
        boundary = now.replace(minute=(now.minute // minutes) * minutes, second=0, microsecond=0)
        if self._state.last_bar_boundary == boundary:
            return []

        df1 = await _fetch_1m(self.ticker)
        if df1 is None or df1.empty:
            return []  # boundary not recorded — retried next poll

        ts = pd.to_datetime(df1["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        df1 = df1.assign(timestamp=ts)

        # Closed bars only: drop the in-progress bar so a transient tick
        # can't false-fire (same rationale as bb_watcher).
        df = df1[(df1["timestamp"].dt.tz_convert(ET) < boundary).values]
        df5 = _resample_5m(df)
        self._state.last_bar_boundary = boundary

        alerts: list[VolumeAlert] = []
        for alert in self.evaluate(df5):
            last = self._last_alert
            if last is not None and (now - last).total_seconds() < self.cooldown_sec:
                continue
            self._last_alert = now
            alerts.append(alert)
        return alerts
