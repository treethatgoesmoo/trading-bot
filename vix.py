"""VIX (^VIX) — the market's "fear gauge", which moves inversely to SPY.

Two uses (see config VIX_*):
  1. A confirming forecast signal: VIX falling => SPY bullish, VIX rising =>
     SPY bearish. Folded into strategy.compute_30m_forecast as a weighted vote.
  2. Intraday spike alerts: ping on a sudden VIX surge (risk-off) or when it
     crosses a regime level (elevated/high/fear) — a potential SPY-reversal warn.

VIX is fetched directly from yfinance (^VIX) so it never competes with the
ticker data sources / API budgets.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import config as cfg

ET = pytz.timezone("US/Eastern")


def regime(level: float) -> str:
    """Volatility regime label for a VIX level."""
    if level < 15:
        return "CALM"
    if level < cfg.VIX_ELEVATED:
        return "NORMAL"
    if level < cfg.VIX_HIGH:
        return "ELEVATED"
    if level < cfg.VIX_FEAR:
        return "HIGH"
    return "FEAR"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close"]]


async def _fetch(interval: str, period: str) -> pd.DataFrame | None:
    def _dl():
        df = yf.Ticker("^VIX").history(period=period, interval=interval)
        return None if df is None or df.empty else _normalize(df)
    try:
        return await asyncio.to_thread(_dl)
    except Exception:
        return None


@dataclass
class VixSnapshot:
    level: float
    prev_close: float
    change: float
    change_pct: float
    direction: str   # RISING | FALLING | FLAT (intraday)
    regime: str


async def get_snapshot() -> VixSnapshot | None:
    intraday = await _fetch("5m", "1d")
    daily = await _fetch("1d", "5d")
    if intraday is None or daily is None or intraday.empty or len(daily) < 2:
        return None
    closes = intraday["close"].astype(float)
    level = round(float(closes.iloc[-1]), 2)
    prev_close = round(float(daily["close"].astype(float).iloc[-2]), 2)
    change = round(level - prev_close, 2)
    change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0

    # Intraday direction over ~30 min (6 x 5-min bars).
    ref = closes.iloc[-7] if len(closes) >= 7 else closes.iloc[0]
    delta = level - float(ref)
    direction = "RISING" if delta > 0.1 else "FALLING" if delta < -0.1 else "FLAT"
    return VixSnapshot(level, prev_close, change, change_pct, direction, regime(level))


async def forecast_signal() -> str | None:
    """VIX momentum over the last two 30-min bars (~30-60 min depending on how
    much of the current bar has formed) -> SPY signal (inverse).
    Returns BULLISH/BEARISH/None."""
    df = await _fetch("30m", "5d")
    if df is None or len(df) < 4:
        return None
    closes = df["close"].astype(float)
    ref = float(closes.iloc[-3])
    if ref <= 0:
        return None
    pct = (float(closes.iloc[-1]) - ref) / ref * 100
    if pct > cfg.VIX_SIGNAL_DEADBAND_PCT:
        return "BEARISH"   # VIX rising -> SPY bearish
    if pct < -cfg.VIX_SIGNAL_DEADBAND_PCT:
        return "BULLISH"   # VIX falling -> SPY bullish
    return None


@dataclass
class VixAlert:
    kind: str         # LEVEL | SPIKE
    direction: str    # BEARISH (risk-off) | BULLISH (calming)
    level: float
    change_pct: float
    message: str


@dataclass
class _WatchState:
    band: float | None = None       # highest regime level currently exceeded
    last_spike_ts: object = None
    primed: bool = False


class VixWatcher:
    """Single market-wide watcher for VIX regime crossings and rapid spikes."""

    def __init__(self):
        self.levels = sorted([cfg.VIX_ELEVATED, cfg.VIX_HIGH, cfg.VIX_FEAR])
        self._state = _WatchState()

    def reset(self) -> None:
        self._state = _WatchState()

    def _label(self, lvl: float) -> str:
        return {cfg.VIX_ELEVATED: "elevated", cfg.VIX_HIGH: "high", cfg.VIX_FEAR: "fear"}.get(lvl, str(lvl))

    async def check(self) -> list[VixAlert]:
        df = await _fetch("5m", "1d")
        if df is None or len(df) < cfg.VIX_SPIKE_WINDOW_BARS + 1:
            return []
        closes = df["close"].astype(float)
        cur = round(float(closes.iloc[-1]), 2)
        st = self._state
        alerts: list[VixAlert] = []

        # Highest threshold currently exceeded (for level-cross detection).
        exceeded = max([l for l in self.levels if cur >= l], default=0.0)
        prev = st.band
        st.band = exceeded
        if not st.primed:
            st.primed = True  # establish baseline silently — no startup ping
        elif prev is not None and exceeded != prev:
            if exceeded > prev:  # crossed UP into a higher regime — risk-off
                alerts.append(VixAlert("LEVEL", "BEARISH", cur, 0.0,
                    f"VIX crossed into **{self._label(exceeded)}** ({cur:.1f}) — risk-off, SPY headwind"))
            elif exceeded < prev:  # crossed back DOWN — calming
                lbl = self._label(prev)
                alerts.append(VixAlert("LEVEL", "BULLISH", cur, 0.0,
                    f"VIX fell back below {prev:.0f} ({cur:.1f}) — volatility easing, SPY tailwind"))

        # Rapid intraday spike over the window.
        k = cfg.VIX_SPIKE_WINDOW_BARS
        ref = float(closes.iloc[-(k + 1)])
        if ref > 0:
            pct = (cur - ref) / ref * 100
            ts = df["timestamp"].iloc[-1]
            recent = st.last_spike_ts is not None and (ts - st.last_spike_ts).total_seconds() < cfg.VIX_SPIKE_COOLDOWN_SEC
            if pct >= cfg.VIX_SPIKE_PCT and not recent:
                st.last_spike_ts = ts
                alerts.append(VixAlert("SPIKE", "BEARISH", cur, round(pct, 1),
                    f"VIX spiked +{pct:.1f}% to {cur:.1f} in ~{k * 5} min — fear surging, watch SPY reversal"))
        return alerts
