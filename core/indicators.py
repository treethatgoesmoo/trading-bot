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


def bollinger(closes: pd.Series, period: int = 20,
              num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger bands: (upper, middle, lower). Middle is the SMA(period);
    bands are +/- num_std rolling standard deviations (population, ddof=0 —
    the convention used by most charting platforms)."""
    mid = closes.rolling(window=period, min_periods=period).mean()
    std = closes.rolling(window=period, min_periods=period).std(ddof=0)
    return mid + num_std * std, mid, mid - num_std * std


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


# ── Candlestick patterns ─────────────────────────────────────────────────
# Named 1-3 candle patterns over completed OHLC bars. Definitions are the
# common practical ones, with explicit thresholds so a backtest and the live
# path can never disagree on what "an engulfing" is. Star patterns use the
# lenient (no-gap-required) form — index ETFs gap most days, so the classic
# gap requirement mostly measures the open auction, not the pattern.

CANDLE_DOJI_BODY_FRAC = 0.10       # body <= 10% of range -> doji
CANDLE_MARUBOZU_BODY_FRAC = 0.85   # body >= 85% of range -> marubozu
CANDLE_WICK_BODY_RATIO = 2.0       # hammer/star: dominant wick >= 2x body
CANDLE_STAR_BODY_FRAC = 0.30       # star middle candle: body <= 30% of range
CANDLE_TREND_LOOKBACK = 5          # bars defining the "prior move" context


def _candle_parts(row) -> tuple[float, float, float, float]:
    """(body, range, upper_wick, lower_wick) — range floored to avoid /0."""
    o, h, l, c = (float(row["open"]), float(row["high"]),
                  float(row["low"]), float(row["close"]))
    return abs(c - o), max(h - l, 1e-9), h - max(o, c), min(o, c) - l


def detect_candlestick_patterns(daily: pd.DataFrame) -> list[dict]:
    """Patterns formed by the LAST completed candle(s) of `daily`.

    `daily` must hold completed bars only (the caller enforces the same
    drop-today's-partial-bar discipline as get_daily_bias) with columns
    open/high/low/close, oldest first, and at least
    CANDLE_TREND_LOOKBACK + 3 rows for the trend context.

    Returns a list of {"name", "direction", "candles"} —
    direction "BULLISH"/"BEARISH" for directional patterns, None for
    indecision reads (Doji, Inside Bar). Reversal patterns (hammer,
    shooting star, stars) only fire against a prior move in the opposite
    direction — a hammer mid-air is not a hammer. A bar can legitimately
    match more than one pattern; all matches are returned.
    """
    if len(daily) < CANDLE_TREND_LOOKBACK + 3:
        return []

    c3 = daily.iloc[-1]  # last completed candle
    c2 = daily.iloc[-2]
    c1 = daily.iloc[-3]
    body3, range3, upper3, lower3 = _candle_parts(c3)
    body2, range2, _, _ = _candle_parts(c2)
    body1, range1, _, _ = _candle_parts(c1)
    o3, cl3 = float(c3["open"]), float(c3["close"])
    o2, cl2 = float(c2["open"]), float(c2["close"])
    o1, cl1 = float(c1["open"]), float(c1["close"])
    green3, green2, green1 = cl3 > o3, cl2 > o2, cl1 > o1

    # Prior-move context: the close of the bar before the last candle vs the
    # close CANDLE_TREND_LOOKBACK bars earlier — the swing the reversal
    # patterns need to be reversing.
    closes = daily["close"].astype(float)
    prior_ref = float(closes.iloc[-2 - CANDLE_TREND_LOOKBACK])
    down_move = cl2 < prior_ref
    up_move = cl2 > prior_ref

    hits: list[dict] = []

    def hit(name: str, direction: str | None, candles: int) -> None:
        hits.append({"name": name, "direction": direction, "candles": candles})

    body_frac3 = body3 / range3

    # 1-candle
    if body_frac3 <= CANDLE_DOJI_BODY_FRAC:
        hit("Doji", None, 1)
    if body_frac3 >= CANDLE_MARUBOZU_BODY_FRAC:
        hit("Bullish Marubozu" if green3 else "Bearish Marubozu",
            "BULLISH" if green3 else "BEARISH", 1)
    if float(c3["high"]) < float(c2["high"]) and float(c3["low"]) > float(c2["low"]):
        hit("Inside Bar", None, 1)
    if body_frac3 > CANDLE_DOJI_BODY_FRAC:  # a doji is not also a hammer
        if (lower3 >= CANDLE_WICK_BODY_RATIO * body3 and upper3 <= body3 and down_move):
            hit("Hammer", "BULLISH", 1)
        if (upper3 >= CANDLE_WICK_BODY_RATIO * body3 and lower3 <= body3 and up_move):
            hit("Shooting Star", "BEARISH", 1)

    # 2-candle: real-body engulfing of opposite colors
    if body2 > 0 and body3 > body2:
        if green3 and not green2 and o3 <= cl2 and cl3 >= o2:
            hit("Bullish Engulfing", "BULLISH", 2)
        if not green3 and green2 and o3 >= cl2 and cl3 <= o2:
            hit("Bearish Engulfing", "BEARISH", 2)

    # 3-candle stars: strong bar, small pause bar, strong close back through
    # the midpoint of the first bar's body.
    mid1 = (o1 + cl1) / 2
    strong1 = body1 / range1 >= 0.5
    small2 = body2 / range2 <= CANDLE_STAR_BODY_FRAC
    if strong1 and small2 and not green1 and green3 and cl3 > mid1 and down_move:
        hit("Morning Star", "BULLISH", 3)
    if strong1 and small2 and green1 and not green3 and cl3 < mid1 and up_move:
        hit("Evening Star", "BEARISH", 3)

    return hits
