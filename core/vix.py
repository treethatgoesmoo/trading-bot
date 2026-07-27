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
    # "Previous close" is the last completed day BEFORE the level's own day.
    # Outside market hours there's no today-row, so iloc[-2] would silently
    # compare yesterday against two days ago.
    daily_closes = daily["close"].astype(float)
    last_daily_ts = pd.Timestamp(daily["timestamp"].iloc[-1])
    if last_daily_ts.tzinfo is None:
        last_daily_date = last_daily_ts.date()
    else:
        last_daily_date = last_daily_ts.tz_convert(ET).date()
    today = pd.Timestamp.now(tz=ET).date()
    prev_idx = -2 if last_daily_date == today else -1
    prev_close = round(float(daily_closes.iloc[prev_idx]), 2)
    change = round(level - prev_close, 2)
    change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0

    # Intraday direction over ~30 min (6 x 5-min bars).
    ref = closes.iloc[-7] if len(closes) >= 7 else closes.iloc[0]
    delta = level - float(ref)
    direction = "RISING" if delta > 0.1 else "FALLING" if delta < -0.1 else "FLAT"
    return VixSnapshot(level, prev_close, change, change_pct, direction, regime(level))


def _signal_from_closes(closes: pd.Series) -> str | None:
    """Pure core of forecast_signal (split out for offline testing).

    VIX change over the last ~30 min (6 x 5m bars), gated by the day's own
    volatility: the move must be >= VIX_SIGNAL_MIN_Z standard deviations of
    today's rolling 30-min changes to vote, else abstain. Early in the day
    (too few samples for a std) falls back to the fixed percentage deadband.
    """
    window = 6  # ~30 min of 5m bars
    if len(closes) < window + 2:
        return None
    diffs = closes.astype(float).diff(window).dropna()
    if diffs.empty:
        return None
    delta = float(diffs.iloc[-1])
    if len(diffs) >= window:
        sigma = float(diffs.std())
        if not np.isfinite(sigma) or sigma <= 0:
            return None
        if abs(delta) < cfg.VIX_SIGNAL_MIN_Z * sigma:
            return None
    else:
        ref = float(closes.iloc[-(window + 1)])
        if ref <= 0 or abs(delta) / ref * 100 < cfg.VIX_SIGNAL_DEADBAND_PCT:
            return None
    return "BEARISH" if delta > 0 else "BULLISH"  # inverse: VIX up -> SPY down


async def forecast_signal() -> str | None:
    """VIX momentum -> SPY signal (inverse). Returns BULLISH/BEARISH/None.

    Rebuilt after grading 47% over 329 live votes: the old read compared a
    partially-formed 30-min bar against one from 60-90 minutes earlier with a
    fixed 0.3% deadband (~0.05 VIX pts), so it voted nearly every window on
    stale noise. Now: fresh 5-min bars, and the move must clear the day's own
    volatility (z-gate) to vote at all — literature says VIX is the strongest
    short-horizon SPY predictor (arXiv 2112.15108), so the spec deserved care.
    """
    df = await _fetch("5m", "1d")
    if df is None:
        return None
    return _signal_from_closes(df["close"])


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
        if not st.primed:
            st.primed = True  # establish baseline silently — no startup ping
            st.band = exceeded
        elif prev is not None and exceeded > prev:  # crossed UP into a higher regime — risk-off
            st.band = exceeded
            alerts.append(VixAlert("LEVEL", "BEARISH", cur, 0.0,
                f"VIX crossed into **{self._label(exceeded)}** ({cur:.1f}) — risk-off, SPY headwind"))
        elif prev is not None and exceeded < prev and cur < prev - cfg.VIX_LEVEL_HYSTERESIS:
            # Crossed back DOWN — only past the hysteresis band, so hovering
            # at 19.9↔20.2 doesn't ping BEARISH/BULLISH in alternation.
            st.band = exceeded
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
