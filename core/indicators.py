"""Shared indicator math — pure functions over pandas/numpy, no network.

Used by both the intraday (zero_dte) and swing features so the two never
disagree on what "RSI" or "a pivot" means.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI as a full series."""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def pivots(vals: np.ndarray, order: int) -> tuple[list[int], list[int]]:
    """Indices of local maxima and minima (strict over a +/- order window)."""
    highs, lows = [], []
    n = len(vals)
    for i in range(order, n - order):
        w = vals[i - order:i + order + 1]
        if vals[i] == w.max() and vals[i] > vals[i - 1]:
            highs.append(i)
        if vals[i] == w.min() and vals[i] < vals[i - 1]:
            lows.append(i)
    return highs, lows


def dow_trend(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
              order: int = 2) -> tuple[list[str | None], list[tuple[int, str]]]:
    """Dow Theory trend state with failure-swing reversal events, walk-forward.

    Uptrend = successive higher swing highs AND higher swing lows; it stands
    until reversed. A BEARISH reversal fires on the bar where, after a LOWER
    swing high has formed (failure to extend the trend), the close breaks
    below the last confirmed swing low. BULLISH is the mirror: a higher swing
    low followed by a close above the last confirmed swing high.

    No lookahead: a pivot found with a +/-`order` window only becomes a
    confirmed swing `order` bars after it prints, so states[i] and any event
    at bar i use only data available at bar i. Returns (states, events) —
    states is per-bar "UP"/"DOWN"/None (None until structure establishes),
    events is [(bar_index, "BULLISH"|"BEARISH"), ...].
    """
    swing_high_idx = pivots(np.asarray(highs, dtype=float), order)[0]
    swing_low_idx = pivots(np.asarray(lows, dtype=float), order)[1]
    sh: list[float] = []   # confirmed swing high values, chronological
    sl: list[float] = []   # confirmed swing low values, chronological
    next_h = next_l = 0
    state: str | None = None
    states: list[str | None] = []
    events: list[tuple[int, str]] = []
    for i in range(len(closes)):
        while next_h < len(swing_high_idx) and swing_high_idx[next_h] + order <= i:
            sh.append(float(highs[swing_high_idx[next_h]]))
            next_h += 1
        while next_l < len(swing_low_idx) and swing_low_idx[next_l] + order <= i:
            sl.append(float(lows[swing_low_idx[next_l]]))
            next_l += 1
        if len(sh) >= 2 and len(sl) >= 2:
            if state is None:
                if sh[-1] > sh[-2] and sl[-1] > sl[-2]:
                    state = "UP"
                elif sh[-1] < sh[-2] and sl[-1] < sl[-2]:
                    state = "DOWN"
            elif state == "UP":
                if sh[-1] < sh[-2] and closes[i] < sl[-1]:
                    state = "DOWN"
                    events.append((i, "BEARISH"))
            else:
                if sl[-1] > sl[-2] and closes[i] > sh[-1]:
                    state = "UP"
                    events.append((i, "BULLISH"))
        states.append(state)
    return states, events


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd(closes: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range over an OHLC DataFrame (columns high/low/close)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-balance volume (columns close/volume)."""
    closes = df["close"].astype(float)
    volume = df["volume"].astype(float)
    direction = np.sign(closes.diff()).fillna(0.0)
    return (direction * volume).cumsum()
