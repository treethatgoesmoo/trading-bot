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
    return load_jsonl("forecasts", date_str)


# ── Generic JSONL helpers ────────────────────────────────────────────────

def _append_jsonl(kind: str, date_str: str, entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_jsonl(kind: str, date_str: str) -> list[dict]:
    """Load a per-day JSONL file (forecasts, bias, outcomes, filters, votes)."""
    path = os.path.join(DATA_DIR, f"{kind}-{date_str}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


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


def available_dates(kind: str = "results") -> list[str]:
    """Sorted list of dates (YYYY-MM-DD) for which a given data file exists."""
    if not os.path.isdir(DATA_DIR):
        return []
    prefix = f"{kind}-"
    dates = []
    for fname in os.listdir(DATA_DIR):
        if fname.startswith(prefix) and (fname.endswith(".jsonl") or fname.endswith(".json")):
            stem = fname[len(prefix):].rsplit(".", 1)[0]
            if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
                dates.append(stem)
    return sorted(dates)


# ── Outcome / bias / filter persistence ──────────────────────────────────

def log_trade_outcome(outcome, now: datetime) -> None:
    """Persist a resolved trade (target hit / stopped / expired) for win-rate history."""
    risk = abs(outcome.entry_price - outcome.stop_loss)
    r_multiple = round(outcome.pnl / risk, 2) if risk else 0.0
    _append_jsonl("outcomes", now.strftime("%Y-%m-%d"), {
        "ticker": outcome.ticker,
        "time": now.isoformat(),
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
    _append_jsonl("bias", now.strftime("%Y-%m-%d"), {
        "ticker": bias.ticker,
        "time": now.isoformat(),
        "direction": bias.direction,
        "trend": bias.trend,
        "gap_direction": bias.gap_direction,
        "gap_pct": bias.gap_pct,
        "multi_day": bias.multi_day,
        "current_price": bias.current_price,
        "sma_value": bias.sma_value,
        "rsi": bias.rsi,
    })


def log_filters(ticker: str, direction: str, filters: list, fired: bool, now: datetime) -> None:
    """Persist the filter pass/fail breakdown for a setup (fired or filtered)."""
    _append_jsonl("filters", now.strftime("%Y-%m-%d"), {
        "ticker": ticker,
        "time": now.isoformat(),
        "direction": direction,
        "fired": fired,
        "filters": [{"name": f.name, "passed": f.passed, "reason": f.reason} for f in filters],
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
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


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
