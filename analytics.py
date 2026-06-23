"""Aggregate the bot's stored history into performance metrics.

Pure functions over the JSON/JSONL files written by forecast_tracker — no
network, no Discord. Both the HTML dashboard (dashboard.py) and the in-chat
`!stats` command build on `build_report()`.

The headline diagnostic is per-indicator accuracy: because every forecast
stores each indicator's BULLISH/BEARISH/NEUTRAL signal *and* the actual move
is recorded at end of day, we can measure which indicators are genuinely
predictive vs. noise — exactly what's needed to tune the algorithm.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import forecast_tracker as ft

# Reference weights used by strategy.compute_30m_forecast (kept in sync manually).
# Shown alongside accuracy so an indicator that's heavily weighted but inaccurate
# stands out as a tuning target.
INDICATOR_WEIGHTS = {
    "RSI": 3.0, "EMA": 2.5, "VWAP": 2.0, "MACD": 2.0,
    "MACD_Mom": 0.5, "Divergence": 3.0, "Volume": 1.0, "Momentum": 1.0,
}

DIRECTIONAL = ("BULLISH", "BEARISH")


def _pct(correct: int, total: int) -> float:
    return round(correct / total * 100, 1) if total else 0.0


# ── Data collection across a date range ──────────────────────────────────

def all_dates() -> list[str]:
    """Union of every date for which we have any data file, sorted ascending."""
    seen: set[str] = set()
    for kind in ("results", "forecasts", "bias", "outcomes", "filters"):
        seen.update(ft.available_dates(kind))
    return sorted(seen)


def _collect(dates: list[str]) -> dict:
    results, bias, outcomes, filters = [], [], [], []
    for d in dates:
        for r in ft.load_results(d):
            r["date"] = d
            results.append(r)
        for b in ft.load_jsonl("bias", d):
            b["date"] = d
            bias.append(b)
        for o in ft.load_jsonl("outcomes", d):
            o["date"] = d
            outcomes.append(o)
        for f in ft.load_jsonl("filters", d):
            f["date"] = d
            filters.append(f)
    return {"results": results, "bias": bias, "outcomes": outcomes, "filters": filters}


# ── Individual metrics ───────────────────────────────────────────────────

def forecast_stats(results: list[dict]) -> dict:
    scored = [r for r in results if r.get("correct") is not None]
    correct = sum(1 for r in scored if r["correct"])
    by_dir = {}
    for d in DIRECTIONAL:
        sub = [r for r in scored if r["direction"] == d]
        by_dir[d] = {"n": len(sub), "correct": sum(1 for r in sub if r["correct"]),
                     "accuracy": _pct(sum(1 for r in sub if r["correct"]), len(sub))}
    confidences = [r["confidence"] for r in scored]
    return {
        "total": len(results),
        "scored": len(scored),
        "correct": correct,
        "accuracy": _pct(correct, len(scored)),
        "neutral": sum(1 for r in results if r.get("direction") == "NEUTRAL"),
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0.0,
        "by_direction": by_dir,
    }


def per_indicator_accuracy(results: list[dict]) -> list[dict]:
    """For each indicator, how often its (non-neutral) signal matched the actual move."""
    agg = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in results:
        actual = r.get("actual")
        if actual not in DIRECTIONAL:
            continue
        for name, signal in (r.get("signals") or {}).items():
            if signal in DIRECTIONAL:
                agg[name]["n"] += 1
                agg[name]["correct"] += (signal == actual)
    rows = []
    for name, a in agg.items():
        rows.append({
            "indicator": name,
            "n": a["n"],
            "correct": a["correct"],
            "accuracy": _pct(a["correct"], a["n"]),
            "weight": INDICATOR_WEIGHTS.get(name),
        })
    rows.sort(key=lambda x: x["accuracy"], reverse=True)
    return rows


def calibration(results: list[dict]) -> list[dict]:
    """Are the confidence numbers honest? Bucket by confidence vs realized hit rate."""
    buckets = [(0, 55), (55, 65), (65, 75), (75, 85), (85, 101)]
    out = []
    for lo, hi in buckets:
        sub = [r for r in results if r.get("correct") is not None and lo <= r["confidence"] < hi]
        if not sub:
            out.append({"label": f"{lo}-{hi if hi <= 100 else 100}%", "n": 0,
                        "predicted": None, "actual": None})
            continue
        correct = sum(1 for r in sub if r["correct"])
        out.append({
            "label": f"{lo}-{hi if hi <= 100 else 100}%",
            "n": len(sub),
            "predicted": round(sum(r["confidence"] for r in sub) / len(sub), 1),
            "actual": _pct(correct, len(sub)),
        })
    return out


def _group_accuracy(results: list[dict], keyfn) -> list[dict]:
    agg = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in results:
        if r.get("correct") is None:
            continue
        k = keyfn(r)
        agg[k]["n"] += 1
        agg[k]["correct"] += r["correct"]
    rows = [{"key": k, "n": v["n"], "correct": v["correct"], "accuracy": _pct(v["correct"], v["n"])}
            for k, v in agg.items()]
    return rows


def accuracy_by_hour(results: list[dict]) -> list[dict]:
    def hour(r):
        try:
            return datetime.fromisoformat(r["time"]).strftime("%I:%M %p").lstrip("0")
        except Exception:
            return "?"
    rows = _group_accuracy(results, hour)
    rows.sort(key=lambda x: datetime.strptime(x["key"], "%I:%M %p") if x["key"] != "?" else datetime.max)
    return rows


def accuracy_by_ticker(results: list[dict]) -> list[dict]:
    rows = _group_accuracy(results, lambda r: r.get("ticker", "?"))
    rows.sort(key=lambda x: x["key"])
    return rows


def accuracy_by_day(results: list[dict]) -> list[dict]:
    rows = _group_accuracy(results, lambda r: r.get("date") or r["time"][:10])
    rows.sort(key=lambda x: x["key"])
    return rows


def bias_stats(bias: list[dict]) -> dict:
    scored = [b for b in bias if b.get("correct") is not None]
    correct = sum(1 for b in scored if b["correct"])
    per_ticker = _group_accuracy(
        [{"correct": b["correct"], "ticker": b["ticker"]} for b in scored],
        lambda r: r["ticker"],
    )
    return {
        "total": len(bias),
        "scored": len(scored),
        "correct": correct,
        "accuracy": _pct(correct, len(scored)),
        "by_ticker": sorted(per_ticker, key=lambda x: x["key"]),
    }


def trade_stats(outcomes: list[dict]) -> dict:
    wins = [o for o in outcomes if o["result"] == "TARGET_HIT"]
    losses = [o for o in outcomes if o["result"] == "STOPPED_OUT"]
    expired = [o for o in outcomes if o["result"] == "EXPIRED"]
    decided = len(wins) + len(losses)
    rs = [o.get("r_multiple", 0) for o in outcomes if o["result"] != "EXPIRED"]
    return {
        "total": len(outcomes),
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "win_rate": _pct(len(wins), decided),
        "total_pnl": round(sum(o.get("pnl", 0) for o in outcomes), 2),
        "avg_r": round(sum(rs) / len(rs), 2) if rs else 0.0,
    }


def filter_stats(filters: list[dict]) -> dict:
    """Which filters fail most often, and how often setups get blocked entirely."""
    agg = defaultdict(lambda: {"seen": 0, "failed": 0})
    setups = len(filters)
    blocked = sum(1 for f in filters if not f.get("fired"))
    for ev in filters:
        for flt in ev.get("filters", []):
            agg[flt["name"]]["seen"] += 1
            agg[flt["name"]]["failed"] += (not flt["passed"])
    rows = [{"filter": name, "seen": v["seen"], "failed": v["failed"],
             "fail_rate": _pct(v["failed"], v["seen"])} for name, v in agg.items()]
    rows.sort(key=lambda x: x["fail_rate"], reverse=True)
    return {"setups": setups, "blocked": blocked,
            "block_rate": _pct(blocked, setups), "by_filter": rows}


# ── Top-level report ─────────────────────────────────────────────────────

def build_report(days: int | None = None) -> dict:
    """Aggregate all stored history (optionally only the most recent `days`)."""
    dates = all_dates()
    if days is not None and days > 0:
        dates = dates[-days:]
    data = _collect(dates)
    results = data["results"]
    return {
        "dates": dates,
        "date_range": (dates[0], dates[-1]) if dates else (None, None),
        "forecast": forecast_stats(results),
        "indicators": per_indicator_accuracy(results),
        "calibration": calibration(results),
        "by_hour": accuracy_by_hour(results),
        "by_ticker": accuracy_by_ticker(results),
        "by_day": accuracy_by_day(results),
        "bias": bias_stats(data["bias"]),
        "trades": trade_stats(data["outcomes"]),
        "filters": filter_stats(data["filters"]),
    }
