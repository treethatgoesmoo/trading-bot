"""Persist daily forecasts and evaluate them against actual price action.

Forecasts are appended to data/forecasts-YYYY-MM-DD.jsonl as they fire.
At end of day, each forecast is compared against the actual move over
the following 30 minutes and results are written to
data/results-YYYY-MM-DD.json for future analysis.
"""

import json
import os
from datetime import datetime

import pandas as pd
import pytz

ET = pytz.timezone("US/Eastern")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def log_forecast(ticker: str, forecast, now: datetime) -> None:
    """Append a forecast to today's JSONL file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"forecasts-{now.strftime('%Y-%m-%d')}.jsonl")
    entry = {
        "ticker": ticker,
        "time": now.isoformat(),
        "direction": forecast.direction,
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


def load_forecasts(date_str: str) -> list[dict]:
    path = os.path.join(DATA_DIR, f"forecasts-{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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
        "voted_at": datetime.now(ET).isoformat(),
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
        "time": now.isoformat(),
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

    actual_by_time = {r["time"]: r["actual"] for r in results if r.get("actual")}
    correct = total = 0
    for vote in votes:
        if vote["ticker"] != ticker or not vote.get("user_direction"):
            continue
        actual = actual_by_time.get(vote["forecast_time"])
        if actual is None:
            continue
        total += 1
        if vote["user_direction"] == actual:
            correct += 1
    return correct, total


async def evaluate_forecasts(client, ticker: str, date_str: str) -> list[dict] | None:
    """Compare each of today's forecasts against the actual next-30-min move.

    A forecast made at 10:00 predicts the 10:00-10:30 candle: actual
    direction is that candle's close vs the price at forecast time.
    """
    forecasts = [e for e in load_forecasts(date_str) if e["ticker"] == ticker]
    if not forecasts:
        return None

    data = await client.fetch_candles(ticker, interval="30", count=50)
    if data.empty:
        return None

    timestamps = pd.to_datetime(data["timestamp"])
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("UTC")
    timestamps = timestamps.dt.tz_convert(ET)

    results = []
    for entry in forecasts:
        ftime = datetime.fromisoformat(entry["time"])
        # The candle covering the forecast window (starts at/just before forecast time)
        mask = (timestamps >= ftime - pd.Timedelta(minutes=2)).values
        future = data[mask]
        if future.empty:
            continue
        outcome_close = float(future.iloc[0]["close"])
        actual = "BULLISH" if outcome_close > entry["price"] else "BEARISH"
        result = dict(entry)
        result["actual"] = actual
        result["actual_price"] = round(outcome_close, 2)
        result["move"] = round(outcome_close - entry["price"], 2)
        result["correct"] = entry["direction"] == actual if entry["direction"] != "NEUTRAL" else None
        results.append(result)

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
        with open(out_path, "w") as f:
            json.dump(existing, f, indent=2)

    return results
