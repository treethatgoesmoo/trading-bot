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
