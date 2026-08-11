"""Retune the intraday forecast indicator weights from graded live results.

Reads data/results-*.json (written nightly by forecast_tracker), measures each
indicator's directional accuracy against the actual move — separately per
forecast stream (15m vs the 5m companion), since the same indicator can be
predictive on one horizon and a coin flip on the other — and maps accuracy to
a weight:

    weight = (accuracy - 0.50) * 20, floored at TUNED_WEIGHT_FLOOR

so 55% -> 1.0, 60% -> 2.0, 65% -> 3.0. The floor keeps underperformers to a
token vote instead of zero: a 0-weight dissenter disappears from the
confidence denominator and the VOLATILE conflict guard, letting a shrunken
ensemble print 100% confidence. Indicators with fewer than --min-samples
graded bars keep their current default weight (not enough evidence to move
them).

Usage:
    python tune_weights.py               # report only
    python tune_weights.py --days 20     # limit to the last 20 result days
    python tune_weights.py --write       # save data/indicator_weights.json

The bot picks up data/indicator_weights.json live (strategy._weight_overrides
reloads it on file change) — no restart needed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime

import pytz

from config import TUNED_WEIGHT_FLOOR
from core.paths import DATA_DIR
from core.tracking import available_dates
from zero_dte.tracker import load_results
from zero_dte.strategy import INDICATOR_WEIGHTS

ET = pytz.timezone("US/Eastern")
DIRECTIONAL = ("BULLISH", "BEARISH")
WEIGHTS_PATH = os.path.join(DATA_DIR, "indicator_weights.json")


def indicator_stats(dates: list[str]) -> dict[str, dict[str, dict]]:
    """Per-stream accuracy: {interval: {indicator: {n, correct}}}.

    The 5m companion stream grades very differently from the main 15m stream
    (most indicators are coin flips on 5m windows but not on 15m), so pooling
    them lets the much larger 5m sample drag every weight to the floor.
    Rows graded before 2026-07-27 carry no interval label — an inseparable
    5m/15m mix — and are excluded rather than guessed at.
    """
    agg: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "correct": 0}))
    for d in dates:
        for r in load_results(d):
            interval = r.get("interval")
            if interval is None:
                continue
            actual = r.get("actual")
            if actual not in DIRECTIONAL:
                continue
            for name, signal in (r.get("signals") or {}).items():
                if signal in DIRECTIONAL:
                    s = agg[str(interval)][name]
                    s["n"] += 1
                    s["correct"] += (signal == actual)
    return {iv: dict(names) for iv, names in agg.items()}


def suggest_weights(stats: dict[str, dict], min_samples: int) -> dict[str, float]:
    weights = dict(INDICATOR_WEIGHTS)
    for name, s in stats.items():
        if s["n"] < min_samples:
            continue
        acc = s["correct"] / s["n"]
        weights[name] = round(max((acc - 0.50) * 20, TUNED_WEIGHT_FLOOR), 1)
    return weights


def retune(days: int = 0, min_samples: int = 20, write: bool = False) -> dict | None:
    """Compute (and optionally persist) tuned weights from graded results.

    Returns the payload dict, or None when there's no graded history yet.
    Called by the CLI below and by the bot's nightly housekeeping cycle.
    """
    dates = available_dates("results")
    if days > 0:
        dates = dates[-days:]
    if not dates:
        return None
    stats = indicator_stats(dates)
    if not stats:
        return None
    payload = {
        "generated": datetime.now(pytz.UTC).isoformat(),
        "days": len(dates),
        "date_range": [dates[0], dates[-1]],
        "min_samples": min_samples,
        # Nested per-interval schema ({"15": {...}, "5": {...}}); the live
        # loader (strategy._weight_overrides) detects it and picks the set
        # matching the stream it's forecasting.
        "samples": {iv: {k: v["n"] for k, v in s.items()} for iv, s in stats.items()},
        "weights": {iv: suggest_weights(s, min_samples) for iv, s in stats.items()},
    }
    if write:
        tmp = WEIGHTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, WEIGHTS_PATH)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=0,
                    help="use only the last N result days (default: all)")
    ap.add_argument("--min-samples", type=int, default=20,
                    help="graded bars required before an indicator's weight moves (default: 20)")
    ap.add_argument("--write", action="store_true",
                    help="save the suggested weights to data/indicator_weights.json")
    args = ap.parse_args()

    dates = available_dates("results")
    if args.days > 0:
        dates = dates[-args.days:]
    if not dates:
        print("No results-*.json files found -- run the bot (or backfill.py) first.")
        return

    stats = indicator_stats(dates)
    if not stats:
        print(f"No graded indicator signals in {len(dates)} result day(s) "
              "(interval-labeled rows only; pre-2026-07-27 grades are excluded).")
        return

    print(f"Indicator accuracy over {len(dates)} day(s) ({dates[0]} to {dates[-1]}):")
    for interval in sorted(stats, key=lambda k: -int(k)):
        s_int = stats[interval]
        suggested = suggest_weights(s_int, args.min_samples)
        print(f"\n[{interval}-min stream]")
        print(f"{'indicator':<12} {'graded':>6} {'accuracy':>9} {'current':>8} {'suggested':>10}")
        for name in sorted(s_int, key=lambda k: -(s_int[k]['correct'] / s_int[k]['n'])):
            s = s_int[name]
            acc = s["correct"] / s["n"]
            cur = INDICATOR_WEIGHTS.get(name, 0.0)
            new = suggested.get(name, cur)
            note = "  (kept -- too few samples)" if s["n"] < args.min_samples else ""
            print(f"{name:<12} {s['n']:>6} {acc:>8.1%} {cur:>8.1f} {new:>10.1f}{note}")

    if args.write:
        retune(args.days, args.min_samples, write=True)
        print(f"\nWrote {WEIGHTS_PATH} -- the bot will pick it up on its next forecast.")
    else:
        print("\nDry run -- pass --write to save data/indicator_weights.json.")


if __name__ == "__main__":
    main()
