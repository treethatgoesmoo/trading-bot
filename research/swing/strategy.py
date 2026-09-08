"""Historical daily-timeframe weighted-vote model, not a live feature.

The explicit research.backtest_swing tool evaluates this model over historical
windows. Weights remain readable from data/swing_weights.json for reproduction.
No signal builder, contract picker, scan scheduler or position monitor remains.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

from research.config import TUNED_WEIGHT_FLOOR
from core.indicators import atr, macd, obv, pivots, rsi_series, sma
from core.paths import SWING_WEIGHTS_FILE

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# Default weights. Like the 0DTE INDICATOR_WEIGHTS these are starting points —
# retune from graded live results with tune_weights.py's swing counterpart.
SWING_INDICATOR_WEIGHTS = {
    "Trend": 3.0,         # SMA stack: close>SMA50>SMA200 / inverse; weekly SMA10 tiebreaker
    "RSI_Pullback": 2.5,  # daily RSI(14) pullback that turned back in trend direction
    "Cross": 2.0,         # 20/50 SMA cross within the last 10 bars
    "MACD": 2.0,          # daily histogram sign, boosted on a recent signal-line cross
    "Structure": 1.5,     # higher-highs/higher-lows vs lower-lows via pivots (~40 bars)
    "Volume": 1.0,        # OBV 20-day slope (accumulation vs distribution)
    "Dist52w": 1.0,       # near 52-week high bullish / deep below bearish
}

MIN_BARS = 210  # need SMA200 + a little runway

_weights_cache: tuple[float, dict] | None = None


def _swing_weight_overrides() -> dict:
    """Tuned weights from data/swing_weights.json, reloaded on file change.

    Clamped to TUNED_WEIGHT_FLOOR — see zero_dte.strategy._weight_overrides:
    zero-weight votes vanish from the confidence denominator.
    """
    global _weights_cache
    try:
        mtime = os.path.getmtime(SWING_WEIGHTS_FILE)
    except OSError:
        return {}
    if _weights_cache is None or _weights_cache[0] != mtime:
        try:
            with open(SWING_WEIGHTS_FILE) as f:
                data = json.load(f)
            overrides = {k: max(float(v), TUNED_WEIGHT_FLOOR)
                         for k, v in data.get("weights", data).items()}
            _weights_cache = (mtime, overrides)
            logger.info("Loaded swing weight overrides: %s", overrides)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", SWING_WEIGHTS_FILE, exc)
            return {}
    return _weights_cache[1]


@dataclass
class SwingVote:
    direction: str            # "BULLISH" | "BEARISH" | "NEUTRAL"
    score: float              # net weighted score (bull - bear)
    confidence: float         # 50-100, margin over all reporting indicators
    signals: dict[str, str]   # indicator -> vote (persisted for grading/tuning)
    weights: dict[str, float]
    close: float
    atr14: float
    rsi_d: float | None
    sma50: float | None
    sma200: float | None
    high_52w: float


def evaluate_daily(df: pd.DataFrame) -> SwingVote | None:
    """Weighted directional vote over a daily OHLCV frame (pure, no network).

    Expects columns timestamp/open/high/low/close/volume, oldest->newest,
    with the last row being the most recent COMPLETED daily candle.
    """
    if df is None or len(df) < MIN_BARS:
        return None

    closes = df["close"].astype(float).reset_index(drop=True)
    highs = df["high"].astype(float).reset_index(drop=True)
    volumes = df["volume"].astype(float).reset_index(drop=True)

    close = float(closes.iloc[-1])
    indicator_weights = {**SWING_INDICATOR_WEIGHTS, **_swing_weight_overrides()}

    signals: dict[str, str] = {}
    weights: dict[str, float] = {}

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    sma50_v = float(sma50.iloc[-1])
    sma200_v = float(sma200.iloc[-1])

    # Trend — SMA stack, with a weekly-close tiebreaker when the stack is mixed.
    if close > sma50_v > sma200_v:
        trend = "BULLISH"
    elif close < sma50_v < sma200_v:
        trend = "BEARISH"
    else:
        # Weekly confirmation for free from the same daily frame.
        wk = df.set_index(pd.to_datetime(df["timestamp"]))["close"].astype(float).resample("W-FRI").last().dropna()
        if len(wk) >= 10:
            wk_sma10 = float(wk.rolling(10).mean().iloc[-1])
            trend = "BULLISH" if float(wk.iloc[-1]) > wk_sma10 else "BEARISH"
        else:
            trend = "NEUTRAL"
    signals["Trend"] = trend
    weights["Trend"] = indicator_weights["Trend"]

    # Cross — 20/50 SMA cross within the last 10 bars.
    diff = (sma20 - sma50).iloc[-11:]
    cross = "NEUTRAL"
    if not diff.isna().any():
        signs = np.sign(diff.to_numpy())
        if signs[-1] > 0 and (signs[:-1] <= 0).any():
            cross = "BULLISH"   # golden cross recently
        elif signs[-1] < 0 and (signs[:-1] >= 0).any():
            cross = "BEARISH"   # death cross recently
    signals["Cross"] = cross
    weights["Cross"] = indicator_weights["Cross"]

    # RSI pullback — a dip that turned back up in an uptrend (and the mirror).
    rsi_vals = rsi_series(closes, 14)
    rsi_v = float(rsi_vals.iloc[-1]) if not np.isnan(rsi_vals.iloc[-1]) else None
    rsi_sig = "NEUTRAL"
    if rsi_v is not None and len(rsi_vals) >= 6:
        recent = rsi_vals.iloc[-5:]
        rising = rsi_v > float(rsi_vals.iloc[-2])
        if trend == "BULLISH" and float(recent.min()) <= 55 and rising:
            rsi_sig = "BULLISH"
        elif trend == "BEARISH" and float(recent.max()) >= 45 and not rising:
            rsi_sig = "BEARISH"
        elif rsi_v >= 75:
            rsi_sig = "BEARISH"   # stretched — poor risk/reward for fresh calls
        elif rsi_v <= 25:
            rsi_sig = "BULLISH"
    signals["RSI_Pullback"] = rsi_sig
    weights["RSI_Pullback"] = indicator_weights["RSI_Pullback"]

    # MACD — histogram sign, upgraded to a vote only when meaningful; a recent
    # signal-line cross in the same direction keeps it voting even when small.
    macd_line, signal_line, hist = macd(closes)
    hist_v = float(hist.iloc[-1])
    macd_sig = "BULLISH" if hist_v > 0 else "BEARISH"
    recent_cross = False
    h5 = hist.iloc[-6:].to_numpy()
    if len(h5) >= 2 and (np.sign(h5[:-1]) != np.sign(h5[-1])).any():
        recent_cross = True
    signals["MACD"] = macd_sig
    weights["MACD"] = indicator_weights["MACD"] * (1.25 if recent_cross else 1.0)

    # Structure — higher-highs/higher-lows vs lower over the last ~40 bars.
    window = closes.iloc[-40:].to_numpy()
    p_highs, p_lows = pivots(window, 2)
    structure = "NEUTRAL"
    if len(p_highs) >= 2 and len(p_lows) >= 2:
        hh = window[p_highs[-1]] > window[p_highs[-2]]
        hl = window[p_lows[-1]] > window[p_lows[-2]]
        if hh and hl:
            structure = "BULLISH"
        elif not hh and not hl:
            structure = "BEARISH"
    signals["Structure"] = structure
    weights["Structure"] = indicator_weights["Structure"]

    # Volume — OBV 20-day slope.
    obv_series = obv(df)
    vol_sig = "NEUTRAL"
    if len(obv_series) >= 21:
        obv_slope = float(obv_series.iloc[-1] - obv_series.iloc[-21])
        denom = float(volumes.iloc[-21:].mean())
        if denom > 0 and abs(obv_slope) > denom:  # more than ~1 avg day of net flow
            vol_sig = "BULLISH" if obv_slope > 0 else "BEARISH"
    signals["Volume"] = vol_sig
    weights["Volume"] = indicator_weights["Volume"]

    # Distance from 52-week high.
    high_52w = float(highs.iloc[-252:].max()) if len(highs) >= 252 else float(highs.max())
    dist_pct = (high_52w - close) / high_52w * 100 if high_52w else 0.0
    if dist_pct <= 5.0:
        d52 = "BULLISH"
    elif dist_pct >= 25.0:
        d52 = "BEARISH"
    else:
        d52 = "NEUTRAL"
    signals["Dist52w"] = d52
    weights["Dist52w"] = indicator_weights["Dist52w"]

    # Weighted vote — same confidence model as the 30-min forecast: margin of
    # the winning side over ALL reporting indicators, scaled to 50-100.
    bull_score = sum(weights.get(k, 0) for k, v in signals.items() if v == "BULLISH")
    bear_score = sum(weights.get(k, 0) for k, v in signals.items() if v == "BEARISH")
    total_weight = sum(weights.values())

    if total_weight == 0 or bull_score == bear_score:
        direction, confidence = "NEUTRAL", 50.0
    else:
        direction = "BULLISH" if bull_score > bear_score else "BEARISH"
        margin = abs(bull_score - bear_score) / total_weight
        confidence = round(50.0 + 50.0 * margin, 1)

    atr_series = atr(df, 14)
    atr_v = float(atr_series.iloc[-1]) if not np.isnan(atr_series.iloc[-1]) else 0.0

    return SwingVote(
        direction=direction,
        score=round(bull_score - bear_score, 2),
        confidence=confidence,
        signals=signals,
        weights={k: round(v, 2) for k, v in weights.items()},
        close=round(close, 2),
        atr14=round(atr_v, 2),
        rsi_d=round(rsi_v, 1) if rsi_v is not None else None,
        sma50=round(sma50_v, 2),
        sma200=round(sma200_v, 2),
        high_52w=round(high_52w, 2),
    )


def completed_daily_candles(df: pd.DataFrame, asof: datetime | None = None) -> pd.DataFrame:
    """Return daily candles completed before the ET date containing ``asof``.

    yfinance and the paid providers include today's live daily bar. Keeping
    this boundary in one helper prevents scheduled scans and on-demand
    analysis from silently using different data.
    """
    if df is None or df.empty:
        return df
    timestamps = pd.to_datetime(df["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    asof = asof or datetime.now(ET)
    return df[(timestamps.dt.tz_convert(ET).dt.date < asof.astimezone(ET).date()).values].copy()
