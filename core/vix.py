"""VIX (^VIX) — the market's "fear gauge", which moves inversely to SPY.

One use now (see config VIX_SIGNAL_*/VIX_FORECAST_WEIGHT): a confirming
forecast signal — VIX falling => SPY bullish, VIX rising => SPY bearish.
Folded into strategy.compute_30m_forecast as a weighted vote.

(The standalone VIX spike/regime watcher — intraday pings on a sudden VIX
surge or a level crossing, plus the `!vix` command — was removed. It never
fed anything besides its own alert stream; the forecast vote above is
independent of it and is unaffected. See docs/state-of-the-code.md.)

VIX is fetched directly from yfinance (^VIX) so it never competes with the
ticker data sources / API budgets.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import config as cfg

ET = pytz.timezone("US/Eastern")


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
