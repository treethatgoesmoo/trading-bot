"""Persist daily forecasts and evaluate them against actual price action.

Forecasts are appended to data/forecasts-YYYY-MM-DD.jsonl as they fire.
At end of day, each forecast is compared against the actual move over the
following forecast window (FORECAST_INTERVAL_MINUTES) and results are written
to data/results-YYYY-MM-DD.json for future analysis.

Stored timestamps are UTC ISO (project convention); the file date in each
filename is the ET trading day. Consumers convert to ET for display.

(Morning-bias/break-and-retest/VIX-watcher/candlestick-watcher persistence
and grading functions — log_bias, evaluate_bias, log_filters,
log_trade_outcome, direction_record, log_trade_decision, evaluate_vix_alerts,
evaluate_candle_patterns — were removed along with those live features. Their
historical data/{bias,filters,outcomes,vix_alerts,candle_patterns,candles}-*
files remain on disk for backtest_filters.py / core.analytics.py / manual
review; nothing writes new rows to them going forward. See
docs/state-of-the-code.md.)
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

from research.config import FORECAST_DEADBAND_ATR_MULT, FORECAST_INTERVAL_MINUTES
from core.indicators import atr as _atr_series
from core.paths import DATA_DIR
from research.core.tracking import (
    write_atomic as _write_atomic,
    append_jsonl as _append_jsonl,
    load_jsonl,
    available_dates,
)

ET = pytz.timezone("US/Eastern")


def log_forecast(ticker: str, forecast, now: datetime) -> dict:
    """Append a forecast to today's JSONL file. Returns the stored entry
    (its `time` is the join key for user votes)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"forecasts-{now.astimezone(ET).strftime('%Y-%m-%d')}.jsonl")
    entry = {
        "ticker": ticker,
        "time": now.astimezone(pytz.UTC).isoformat(),
        "interval": getattr(forecast, "interval", FORECAST_INTERVAL_MINUTES),
        "grading_schema": 2,  # v2 stores signal-time ATR for ex-ante deadbands
        "direction": forecast.direction,
        "lean": forecast.lean,
        "confidence": forecast.confidence,
        "price": forecast.current_price,
        "vwap": forecast.vwap,
        "day_high": getattr(forecast, "day_high", None),
        "day_low": getattr(forecast, "day_low", None),
        "rsi_30m": forecast.rsi_30m,
        "ema_direction": forecast.ema_direction,
        "macd_direction": forecast.macd_direction,
        "divergence": forecast.divergence,
        "signals": forecast.signals,
        # Ex-ante volatility scale for grading. Historical rows without this
        # additive field retain the old outcome-candle fallback below.
        "atr": getattr(forecast, "atr", None),
        # Descriptive-only (see zero_dte/strategy.py module note) — logged so
        # a future honest backtest can grade them for real instead of
        # reconstructing from categorical fields, not because they're graded now.
        "momentum_score": getattr(forecast, "momentum_score", None),
        "extension_score": getattr(forecast, "extension_score", None),
        # Display-only higher-timeframe context derived from completed bars.
        "candle_30m_direction": getattr(forecast, "candle_30m_direction", None),
        "candle_1h_direction": getattr(forecast, "candle_1h_direction", None),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def load_forecasts(date_str: str) -> list[dict]:
    return load_jsonl("forecasts", date_str)


def load_results(date_str: str) -> list[dict]:
    """Load the evaluated-forecast results for a day (results-YYYY-MM-DD.json)."""
    path = os.path.join(DATA_DIR, f"results-{date_str}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def evaluate_votes(ticker: str, date_str: str, results: list[dict]) -> tuple[int, int]:
    """Score user votes against actual outcomes. Returns (correct, total).

    Historical reader over already-logged votes-*.jsonl/results-*.json.
    The Discord vote UI and vote writer have been deleted. This function
    does not collect new feedback or grade the current market-state service."""
    path = os.path.join(DATA_DIR, f"votes-{date_str}.jsonl")
    if not os.path.exists(path):
        return 0, 0
    with open(path) as f:
        votes = [json.loads(line) for line in f if line.strip()]

    # FLAT windows don't score votes either way — a directional vote on a
    # noise window is neither right nor wrong.
    actual_by_time = {r["time"]: r["actual"] for r in results
                      if r.get("actual") in ("BULLISH", "BEARISH")}
    correct = total = 0
    for vote in votes:
        # Only directional calls are scorable — an "agree" on a NEUTRAL
        # forecast (older data) can never match a BULLISH/BEARISH actual.
        if vote["ticker"] != ticker or vote.get("user_direction") not in ("BULLISH", "BEARISH"):
            continue
        actual = actual_by_time.get(vote["forecast_time"])
        if actual is None:
            continue
        total += 1
        if vote["user_direction"] == actual:
            correct += 1
    return correct, total


async def evaluate_forecasts(client, ticker: str, date_str: str) -> list[dict] | None:
    """Compare each of the day's forecasts against the actual next-window move.

    A forecast made at 10:00 predicts the next candle of its own interval
    (entries store `interval`; older entries without it are graded on
    FORECAST_INTERVAL_MINUTES): actual direction is that candle's close vs the
    price at forecast time. Works for past dates too (restart catch-up) as
    long as the candle window still covers them.

    Volatility-scaled deadband: windows whose |move| is under
    FORECAST_DEADBAND_ATR_MULT x the signal-time interval ATR(14) grade as
    actual=FLAT. Historical entries without stored signal-time ATR fall back
    to the outcome-candle ATR to preserve catch-up compatibility. A
    directional call on a flat window goes ungraded (noise,
    not a miss) and a NEUTRAL/VOLATILE call on it grades correct. On a
    directional window NEUTRAL/VOLATILE grade wrong — the hedge classes get a
    symmetric payoff, not free wins.
    """
    forecasts = [e for e in load_forecasts(date_str) if e["ticker"] == ticker]
    if not forecasts:
        return None

    results = []
    intervals = sorted({e.get("interval", FORECAST_INTERVAL_MINUTES) for e in forecasts})
    for interval in intervals:
        # Cover the same wall-clock span regardless of interval (120 bars of
        # 15m ≈ 360 bars of 5m) so catch-up grading still reaches back.
        count = max(120, int(120 * FORECAST_INTERVAL_MINUTES / interval))
        data = await client.fetch_candles(ticker, interval=str(interval), count=count)
        if data.empty:
            continue

        timestamps = pd.to_datetime(data["timestamp"])
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize("UTC")
        timestamps = timestamps.dt.tz_convert(ET)

        atr_vals = _atr_series(data, 14)
        atr_fallback = float(atr_vals.dropna().mean()) if atr_vals.notna().any() else 0.0

        for entry in forecasts:
            if entry.get("interval", FORECAST_INTERVAL_MINUTES) != interval:
                continue
            ftime = datetime.fromisoformat(entry["time"])
            # The candle covering the forecast window (starts at/just before forecast time)
            mask = (timestamps >= ftime - pd.Timedelta(minutes=2)).values
            future = data[mask]
            if future.empty:
                continue
            # Provider data gaps must not grade a forecast against a candle far
            # in its future (a whole day once went 1/12 this way) — skip instead.
            candle_ts = timestamps[mask].iloc[0]
            if (candle_ts - ftime).total_seconds() > (interval + 5) * 60:
                continue
            outcome_close = float(future.iloc[0]["close"])
            pos = int(np.argmax(mask))
            signal_atr = entry.get("atr")
            if isinstance(signal_atr, (int, float)) and np.isfinite(signal_atr) and signal_atr > 0:
                band_atr = float(signal_atr)
                atr_source = "signal"
            else:
                band_atr = float(atr_vals.iloc[pos]) if not np.isnan(atr_vals.iloc[pos]) else atr_fallback
                atr_source = "outcome_fallback"
            deadband = FORECAST_DEADBAND_ATR_MULT * band_atr
            move = outcome_close - entry["price"]

            result = dict(entry)
            result["actual_price"] = round(outcome_close, 2)
            result["move"] = round(move, 2)
            result["deadband"] = round(deadband, 3)
            result["deadband_atr_source"] = atr_source
            if abs(move) < deadband:
                # Flat window: a directional call goes ungraded (noise, not a
                # miss); calling no-edge on a no-move window is correct.
                result["actual"] = "FLAT"
                result["correct"] = True if entry["direction"] in ("NEUTRAL", "VOLATILE") else None
            else:
                actual = "BULLISH" if move > 0 else "BEARISH"
                result["actual"] = actual
                # NEUTRAL/VOLATILE grade symmetrically: correct on a flat
                # window (above), wrong on a real move. Leaving them ungraded
                # here while collecting the flat-window wins gave the hedging
                # classes a one-sided payoff that inflated headline accuracy
                # (63.4% reported vs 54.6% directional-only when found).
                result["correct"] = entry["direction"] == actual
            results.append(result)

    if not results:
        return None
    results.sort(key=lambda r: r["time"])  # grouped-by-interval grading above

    if results:
        out_path = os.path.join(DATA_DIR, f"results-{date_str}.json")
        existing = []
        if os.path.exists(out_path):
            try:
                with open(out_path) as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing = [r for r in existing if r.get("ticker") != ticker]
        existing.extend(results)
        _write_atomic(out_path, json.dumps(existing, indent=2))

    return results


# ── Watcher-alert grading (in-place row enrichment) ──────────────────────
# bb_alerts / volume_alerts rows are appended when a ping fires and graded
# after the close by rewriting the day file with actual/correct fields ADDED
# (never removed) — the same in-place precedent as the old bias grading.
# Idempotent: rows already carrying a "correct" key are left untouched, so
# the nightly pass and the startup catch-up can both run safely. Every alert
# stream must grade — an ungraded ping can never earn or lose its place in
# the system (house rule, 2026-08-12).


def _rewrite_jsonl(kind: str, date_str: str, rows: list[dict]) -> None:
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    _write_atomic(path, "".join(json.dumps(r) + "\n" for r in rows))


async def evaluate_bb_alerts(client, date_str: str) -> int:
    """Grade the day's Bollinger touch pings as mean-reversion calls.

    A touch means "stretched vs the mean", so: REVERTED (correct) when a
    later 5m close gets back to the middle band shown on the alert before
    extending a full band-width beyond the touched band; EXTENDED (wrong) on
    the extension; DRIFT when the session ends with neither, graded by which
    side of the touch price it closed on — symmetric, no free passes.
    Historical 1m rows grade on the same 5m bars (the 1m stream was dropped
    2026-08-12).
    """
    rows = load_jsonl("bb_alerts", date_str)
    todo = [r for r in rows if "correct" not in r]
    if not todo:
        return 0
    graded = 0
    frames: dict[str, pd.DataFrame | None] = {}
    for r in todo:
        ticker = r.get("ticker")
        if ticker not in frames:
            try:
                frames[ticker] = await client.fetch_candles(ticker, interval="5", count=400)
            except Exception:
                frames[ticker] = None
        data = frames[ticker]
        if data is None or data.empty:
            continue
        ts = pd.to_datetime(data["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        t0 = datetime.fromisoformat(r["time"])
        day = t0.astimezone(ET).date()
        after = ((ts > pd.Timestamp(t0)) & (ts.dt.tz_convert(ET).dt.date == day)).values
        closes = data["close"].astype(float)[after]
        if closes.empty:
            continue  # bars beyond the provider window — stays ungraded
        price, middle, band = float(r["price"]), float(r["middle"]), float(r["band_value"])
        width = abs(band - middle) or 1e-9
        upper = r.get("band") == "upper"
        resolution = correct = None
        deciding = float(closes.iloc[-1])
        for c in closes:
            c = float(c)
            if (c <= middle) if upper else (c >= middle):
                resolution, correct, deciding = "REVERTED", True, c
                break
            if (c >= band + width) if upper else (c <= band - width):
                resolution, correct, deciding = "EXTENDED", False, c
                break
        if resolution is None:
            resolution = "DRIFT"
            correct = (deciding < price) if upper else (deciding > price)
        r["resolution"] = resolution
        r["graded_close"] = round(deciding, 2)
        r["correct"] = correct
        graded += 1
    if graded:
        _rewrite_jsonl("bb_alerts", date_str, rows)
    return graded


async def evaluate_volume_alerts(client, date_str: str) -> int:
    """Grade the day's volume-spike pings as continuation calls.

    Each alert stores the triggering bar's implied direction (up close =
    BULLISH, down close = BEARISH). Graded against SPY-style next-30-min
    price action on the alert's own ticker, same volatility-deadband rule as
    forecast/BB grading: a real move grades CONTINUED (correct, price kept
    moving the triggering direction) or REVERSED (wrong); a flat window
    grades actual=FLAT with correct=null (ungradeable, not a free win).
    """
    rows = load_jsonl("volume_alerts", date_str)
    todo = [r for r in rows if "correct" not in r]
    if not todo:
        return 0
    graded = 0
    frames: dict[str, pd.DataFrame | None] = {}
    for r in todo:
        ticker = r.get("ticker")
        if ticker not in frames:
            try:
                frames[ticker] = await client.fetch_candles(ticker, interval="5", count=400)
            except Exception:
                frames[ticker] = None
        data = frames[ticker]
        if data is None or data.empty:
            continue
        ts = pd.to_datetime(data["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize("UTC")
        atr_vals = _atr_series(data, 14)
        atr_fallback = float(atr_vals.dropna().mean()) if atr_vals.notna().any() else 0.0
        closes = data["close"].astype(float)

        t0 = pd.Timestamp(datetime.fromisoformat(r["time"]))
        day = t0.tz_convert(ET).date() if t0.tzinfo else None
        base_mask = (ts <= t0).values
        if not base_mask.any():
            continue
        base_close = float(closes[base_mask].iloc[-1])
        day_after = ((ts > t0) & (ts.dt.tz_convert(ET).dt.date == day)).values
        target_mask = (ts >= t0 + pd.Timedelta(minutes=30)).values & day_after
        if target_mask.any():
            pos = int(np.argmax(target_mask))
        elif day_after.any():
            pos = len(day_after) - 1 - int(np.argmax(day_after[::-1]))  # last bar of the day
        else:
            continue  # bars beyond the provider window — stays ungraded
        move = float(closes.iloc[pos]) - base_close
        band_atr = float(atr_vals.iloc[pos]) if not np.isnan(atr_vals.iloc[pos]) else atr_fallback
        deadband = FORECAST_DEADBAND_ATR_MULT * band_atr
        r["move"] = round(move, 2)
        if abs(move) < deadband:
            r["actual"] = "FLAT"
            r["correct"] = None
        else:
            actual = "BULLISH" if move > 0 else "BEARISH"
            r["actual"] = actual
            r["correct"] = r.get("direction") == actual
        graded += 1
    if graded:
        _rewrite_jsonl("volume_alerts", date_str, rows)
    return graded
