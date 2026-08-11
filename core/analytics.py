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
from statistics import median

import pytz

ET = pytz.timezone("US/Eastern")
DIRECTIONAL = ("BULLISH", "BEARISH")

# Feature trackers are imported lazily to keep core free of import-time
# dependencies on feature packages (strict dependency direction).
_ft_mod = None


def _ft():
    global _ft_mod
    if _ft_mod is None:
        from zero_dte import tracker
        _ft_mod = tracker
    return _ft_mod


def _effective_weights(interval: int | None = None) -> dict:
    """The weights strategy.compute_30m_forecast actually uses right now for
    the given stream (defaults + any live-tuned overrides from
    data/indicator_weights.json). Shown alongside accuracy so an indicator
    that's heavily weighted but inaccurate stands out as a tuning target."""
    from zero_dte.strategy import INDICATOR_WEIGHTS, _weight_overrides
    return {**INDICATOR_WEIGHTS, **_weight_overrides(interval)}


def _pct(correct: int, total: int) -> float:
    return round(correct / total * 100, 1) if total else 0.0


# ── Data collection across a date range ──────────────────────────────────

def all_dates() -> list[str]:
    """Union of every date for which we have any data file, sorted ascending."""
    seen: set[str] = set()
    for kind in ("results", "forecasts", "bias", "outcomes", "filters",
                 "swing_signals", "swing_outcomes"):
        seen.update(_ft().available_dates(kind))
    return sorted(seen)


def _collect(dates: list[str]) -> dict:
    results, bias, outcomes, filters = [], [], [], []
    for d in dates:
        for r in _ft().load_results(d):
            r["date"] = d
            results.append(r)
        for b in _ft().load_jsonl("bias", d):
            b["date"] = d
            bias.append(b)
        for o in _ft().load_jsonl("outcomes", d):
            o["date"] = d
            outcomes.append(o)
        for f in _ft().load_jsonl("filters", d):
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
        "volatile": sum(1 for r in results if r.get("direction") == "VOLATILE"),
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0.0,
        "by_direction": by_dir,
    }


def per_indicator_accuracy(results: list[dict]) -> list[dict]:
    """For each (indicator, forecast stream), how often its (non-neutral)
    signal matched the actual move. Split per stream: the 5m companion grades
    very differently from the 15m stream, and pooling them buried that (e.g.
    MACD 57.6% on 15m vs 50.5% on 5m as of 2026-08-11). Rows graded before
    2026-07-27 have no interval label and report as the 'mixed' stream."""
    agg = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in results:
        actual = r.get("actual")
        if actual not in DIRECTIONAL:
            continue
        interval = r.get("interval")
        for name, signal in (r.get("signals") or {}).items():
            if signal in DIRECTIONAL:
                agg[(name, interval)]["n"] += 1
                agg[(name, interval)]["correct"] += (signal == actual)
    weight_sets: dict = {}
    rows = []
    for (name, interval), a in agg.items():
        if interval not in weight_sets:
            weight_sets[interval] = _effective_weights(interval)
        rows.append({
            "indicator": name,
            "stream": f"{interval}m" if interval is not None else "mixed",
            "n": a["n"],
            "correct": a["correct"],
            "accuracy": _pct(a["correct"], a["n"]),
            "weight": weight_sets[interval].get(name),
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
            # Stored times are UTC ISO (older files: ET-offset) — bucket in ET.
            return datetime.fromisoformat(r["time"]).astimezone(ET).strftime("%I:%M %p").lstrip("0")
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


def _swing_effective_weights() -> dict:
    from swing.strategy import SWING_INDICATOR_WEIGHTS, _swing_weight_overrides
    return {**SWING_INDICATOR_WEIGHTS, **_swing_weight_overrides()}


def swing_report(days: int | None = None) -> dict:
    """Aggregate swing signals and graded outcomes (swing_signals/swing_outcomes)."""
    sig_dates = _ft().available_dates("swing_signals")
    out_dates = _ft().available_dates("swing_outcomes")
    if days is not None and days > 0:
        sig_dates = sig_dates[-days:]
        out_dates = out_dates[-days:]
    signals = [e for d in sig_dates for e in _ft().load_jsonl("swing_signals", d)]
    outcomes = [e for d in out_dates for e in _ft().load_jsonl("swing_outcomes", d)]

    wins = sum(1 for o in outcomes if o.get("win"))
    graded = len(outcomes)
    moves = [o["move_pct"] for o in outcomes if o.get("move_pct") is not None]
    opt_pnls = [o["option"]["pnl_pct"] for o in outcomes if o.get("option")]
    # Split by how the exit was priced. A live requote is the real fill only
    # when it was taken the day the signal resolved; delta_est carries no
    # timing error but ignores theta, so it reads optimistic. Averaging them
    # blind hides which bias is in the headline. Records written before the
    # as-of pricing fix have no `quote_lag_days` — count them separately
    # rather than silently folding them in as clean.
    opt_by_method = defaultdict(list)
    for o in outcomes:
        opt = o.get("option") or {}
        if "pnl_pct" not in opt:
            continue
        method = opt.get("pnl_method", "?")
        if "quote_lag_days" not in opt:
            method += "_legacy"
        opt_by_method[method].append(opt["pnl_pct"])

    by_dir = {}
    for d in ("CALL", "PUT"):
        sub = [o for o in outcomes if o["direction"] == d]
        w = sum(1 for o in sub if o.get("win"))
        by_dir[d] = {"n": len(sub), "wins": w, "win_rate": _pct(w, len(sub))}

    by_ticker = _group_accuracy(
        [{"correct": o.get("win", False), "ticker": o["ticker"]} for o in outcomes],
        lambda r: r["ticker"],
    )

    # Per-indicator accuracy: each signal's vote dict vs the graded actual move.
    agg = defaultdict(lambda: {"n": 0, "correct": 0})
    for o in outcomes:
        actual = o.get("actual")
        if actual not in DIRECTIONAL:
            continue
        for name, sig in (o.get("signals") or {}).items():
            if sig in DIRECTIONAL:
                agg[name]["n"] += 1
                agg[name]["correct"] += (sig == actual)
    weights = _swing_effective_weights()
    indicators = [{
        "indicator": name, "n": a["n"], "correct": a["correct"],
        "accuracy": _pct(a["correct"], a["n"]), "weight": weights.get(name),
    } for name, a in agg.items()]
    indicators.sort(key=lambda x: x["accuracy"], reverse=True)

    resolutions = defaultdict(int)
    for o in outcomes:
        resolutions[o.get("resolution", "?")] += 1

    return {
        "signals": len(signals),
        "graded": graded,
        "wins": wins,
        "win_rate": _pct(wins, graded),
        "avg_move": round(sum(moves) / len(moves), 2) if moves else 0.0,
        "avg_option_pnl": round(sum(opt_pnls) / len(opt_pnls), 1) if opt_pnls else None,
        "median_option_pnl": round(median(opt_pnls), 1) if opt_pnls else None,
        "option_pnl_by_method": {
            m: {"n": len(v), "avg": round(sum(v) / len(v), 1), "median": round(median(v), 1)}
            for m, v in sorted(opt_by_method.items())
        },
        "by_direction": by_dir,
        "by_ticker": sorted(by_ticker, key=lambda x: x["key"]),
        "resolutions": dict(resolutions),
        "indicators": indicators,
    }


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
        "swing": swing_report(days),
    }
