"""Persist daily forecasts and evaluate them against actual price action.

Forecasts are appended to data/forecasts-YYYY-MM-DD.jsonl as they fire.
At end of day, each forecast is compared against the actual move over the
following forecast window (FORECAST_INTERVAL_MINUTES) and results are written
to data/results-YYYY-MM-DD.json for future analysis.

Stored timestamps are UTC ISO (project convention); the file date in each
filename is the ET trading day. Consumers convert to ET for display.
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pytz

from config import FORECAST_DEADBAND_ATR_MULT, FORECAST_INTERVAL_MINUTES
from core.indicators import atr as _atr_series
from core.paths import DATA_DIR
from core.tracking import (
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
        "direction": forecast.direction,
        "lean": forecast.lean,
        "confidence": forecast.confidence,
        "price": forecast.current_price,
        "vwap": forecast.vwap,
        "rsi_30m": forecast.rsi_30m,
        "ema_direction": forecast.ema_direction,
        "macd_direction": forecast.macd_direction,
        "divergence": forecast.divergence,
        "signals": forecast.signals,
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


# ── Outcome / bias / filter persistence ──────────────────────────────────

def log_trade_outcome(outcome, now: datetime) -> None:
    """Persist a resolved trade (target hit / stopped / expired) for win-rate history."""
    risk = abs(outcome.entry_price - outcome.stop_loss)
    r_multiple = round(outcome.pnl / risk, 2) if risk else 0.0
    _append_jsonl("outcomes", now.astimezone(ET).strftime("%Y-%m-%d"), {
        "ticker": outcome.ticker,
        "time": now.astimezone(pytz.UTC).isoformat(),
        "direction": outcome.direction,
        "result": outcome.result,            # TARGET_HIT | STOPPED_OUT | EXPIRED
        "entry_price": outcome.entry_price,
        "exit_price": outcome.exit_price,
        "stop_loss": outcome.stop_loss,
        "target": outcome.target,
        "pnl": outcome.pnl,
        "r_multiple": r_multiple,
    })


def log_bias(bias, now: datetime) -> None:
    """Persist the morning daily-bias call so its accuracy can be tracked over time."""
    _append_jsonl("bias", now.astimezone(ET).strftime("%Y-%m-%d"), {
        "ticker": bias.ticker,
        "time": now.astimezone(pytz.UTC).isoformat(),
        "direction": bias.direction,
        "trend": bias.trend,
        "gap_direction": bias.gap_direction,
        "gap_pct": bias.gap_pct,
        "multi_day": bias.multi_day,
        "current_price": bias.current_price,
        "sma_value": bias.sma_value,
        "rsi": bias.rsi,
    })


def log_day_forecast(day_fc, now: datetime) -> None:
    """Persist the morning day forecast (projected high/low) so a mid-day
    restart can restore the exact numbers shown on intraday forecast embeds
    instead of losing them with the process."""
    _append_jsonl("day_forecast", now.astimezone(ET).strftime("%Y-%m-%d"), {
        "ticker": day_fc.ticker,
        "time": now.astimezone(pytz.UTC).isoformat(),
        "direction": day_fc.direction,
        "strength": day_fc.strength,
        "open_price": day_fc.open_price,
        "exp_high": day_fc.exp_high,
        "exp_low": day_fc.exp_low,
        "day_atr": day_fc.day_atr,
        "pivot": day_fc.pivot,
        "r1": day_fc.r1,
        "s1": day_fc.s1,
        "prev_high": day_fc.prev_high,
        "prev_low": day_fc.prev_low,
        "timestamp": day_fc.timestamp,
    })


def log_filters(ticker: str, direction: str, filters: list, fired: bool, now: datetime) -> None:
    """Persist the filter pass/fail breakdown for a setup (fired or filtered)."""
    _append_jsonl("filters", now.astimezone(ET).strftime("%Y-%m-%d"), {
        "ticker": ticker,
        "time": now.astimezone(pytz.UTC).isoformat(),
        "direction": direction,
        "fired": fired,
        "filters": [{"name": f.name, "passed": f.passed, "reason": f.reason,
                     "advisory": f.advisory} for f in filters],
    })


async def evaluate_bias(client, ticker: str, date_str: str) -> None:
    """Mark each of the day's bias records correct/incorrect vs the actual daily move.

    Actual day direction = daily candle close vs open. Rewrites bias-DATE.jsonl
    with `actual_direction` and `correct` added to this ticker's entries.
    """
    entries = load_jsonl("bias", date_str)
    if not entries or all(e.get("correct") is not None for e in entries if e["ticker"] == ticker):
        return

    data = await client.fetch_candles(ticker, interval="D", count=10)
    if data.empty:
        return
    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    day = timestamps.dt.tz_convert(ET).dt.strftime("%Y-%m-%d")
    rows = data[(day == date_str).values]
    if rows.empty:
        return
    o, c = float(rows.iloc[-1]["open"]), float(rows.iloc[-1]["close"])
    actual = "BULLISH" if c > o else "BEARISH"

    changed = False
    for e in entries:
        if e["ticker"] == ticker and e.get("correct") is None:
            e["actual_direction"] = actual
            e["correct"] = (e["direction"] == actual)
            changed = True
    if changed:
        path = os.path.join(DATA_DIR, f"bias-{date_str}.jsonl")
        _write_atomic(path, "".join(json.dumps(e) + "\n" for e in entries))


def log_user_vote(ticker: str, forecast_time_iso: str, bot_direction: str,
                  user_direction: str | None, user: str) -> None:
    """Record a user's agree/disagree vote on a forecast."""
    os.makedirs(DATA_DIR, exist_ok=True)
    date_str = forecast_time_iso[:10]
    path = os.path.join(DATA_DIR, f"votes-{date_str}.jsonl")
    entry = {
        "ticker": ticker,
        "forecast_time": forecast_time_iso,
        "bot_direction": bot_direction,
        "user_direction": user_direction,
        "user": user,
        "voted_at": datetime.now(pytz.UTC).isoformat(),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_trade_decision(ticker: str, signal_info: dict, decision: str, user: str) -> None:
    """Record a user's Take/Skip/Modify decision on a trade signal."""
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(ET)
    path = os.path.join(DATA_DIR, f"trade_decisions-{now.strftime('%Y-%m-%d')}.jsonl")
    entry = {
        "ticker": ticker,
        "decision": decision,
        "user": user,
        "time": now.astimezone(pytz.UTC).isoformat(),
        **signal_info,
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def evaluate_votes(ticker: str, date_str: str, results: list[dict]) -> tuple[int, int]:
    """Score user votes against actual outcomes. Returns (correct, total)."""
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
    FORECAST_DEADBAND_ATR_MULT x the interval ATR(14) at that candle grade as
    actual=FLAT — a directional call on a flat window goes ungraded (noise,
    not a miss) and a NEUTRAL/VOLATILE call on it grades correct.
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
            band_atr = float(atr_vals.iloc[pos]) if not np.isnan(atr_vals.iloc[pos]) else atr_fallback
            deadband = FORECAST_DEADBAND_ATR_MULT * band_atr
            move = outcome_close - entry["price"]

            result = dict(entry)
            result["actual_price"] = round(outcome_close, 2)
            result["move"] = round(move, 2)
            result["deadband"] = round(deadband, 3)
            if abs(move) < deadband:
                # Flat window: a directional call goes ungraded (noise, not a
                # miss); calling no-edge on a no-move window is correct.
                result["actual"] = "FLAT"
                result["correct"] = True if entry["direction"] in ("NEUTRAL", "VOLATILE") else None
            else:
                actual = "BULLISH" if move > 0 else "BEARISH"
                result["actual"] = actual
                # NEUTRAL/VOLATILE on a real move stay on-record but ungraded
                # (their per-indicator votes still feed the tuner).
                result["correct"] = entry["direction"] == actual if entry["direction"] in ("BULLISH", "BEARISH") else None
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
