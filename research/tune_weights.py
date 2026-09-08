"""Retune the intraday forecast indicator weights from graded live results.

Reads data/results-*.json (written nightly by forecast_tracker), measures each
indicator's directional accuracy against the actual move — separately per
forecast stream (15m vs the 5m companion), since the same indicator can be
predictive on one horizon and a coin flip on the other — and maps accuracy to
a weight:

    weight = (shrunk_accuracy - 0.50) * 20, floored at TUNED_WEIGHT_FLOOR

where shrunk_accuracy pulls the measured rate toward 50% by
TUNED_WEIGHT_SHRINKAGE coin-flip pseudo-samples (so 60% off 30 bars barely
moves, 60% off 300 bars mostly counts), and the result may move at most
TUNED_WEIGHT_MAX_STEP per retune from the previously written weight (see
core/tuning.py). The floor keeps underperformers to a token vote instead of
zero: a 0-weight dissenter disappears from the confidence denominator and the
VOLATILE conflict guard, letting a shrunken ensemble print 100% confidence.
Indicators with fewer than --min-samples graded bars keep their current
default weight (not enough evidence to move them).

Usage:
    python -m research.tune_weights               # report only
    python -m research.tune_weights --days 20     # last 20 result days
    python -m research.tune_weights --write       # historical weights only

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

from research.config import TUNED_WEIGHT_FLOOR
from research.core.analytics import latest_grading_rows
from core.paths import DATA_DIR
from research.core.tracking import available_dates
from research.core.tuning import accuracy_to_weight, load_previous_weights, shrunk_accuracy, step_limited
from research.zero_dte.tracker import load_results
from research.zero_dte.strategy import INDICATOR_WEIGHTS

ET = pytz.timezone("US/Eastern")
DIRECTIONAL = ("BULLISH", "BEARISH")
WEIGHTS_PATH = os.path.join(DATA_DIR, "indicator_weights.json")
RETIRED_INDICATORS = {"Divergence"}


def indicator_stats(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Per-stream accuracy: {interval: {indicator: {n, correct}}}.

    The 5m companion stream grades very differently from the main 15m stream
    (most indicators are coin flips on 5m windows but not on 15m), so pooling
    them lets the much larger 5m sample drag every weight to the floor.
    Rows graded before 2026-07-27 carry no interval label — an inseparable
    5m/15m mix — and are excluded rather than guessed at.
    """
    agg: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "correct": 0}))
    for r in rows:
        interval = r.get("interval")
        if interval is None:
            continue
        actual = r.get("actual")
        if actual not in DIRECTIONAL:
            continue
        for name, signal in (r.get("signals") or {}).items():
            if name in RETIRED_INDICATORS:
                continue
            if signal in DIRECTIONAL:
                s = agg[str(interval)][name]
                s["n"] += 1
                s["correct"] += (signal == actual)
    return {iv: dict(names) for iv, names in agg.items()}


def _previous_weights() -> dict:
    return load_previous_weights(WEIGHTS_PATH)


def _prev_for_interval(prev_all: dict, interval: str) -> dict:
    """Per-interval slice of the previous weights (flat legacy files apply
    to every interval)."""
    if prev_all and all(isinstance(v, dict) for v in prev_all.values()):
        return prev_all.get(interval, {})
    return prev_all


def suggest_weights(stats: dict[str, dict], min_samples: int,
                    prev: dict | None = None) -> dict[str, float]:
    """Damped accuracy -> weight mapping: shrinkage toward 50% by sample
    size, then step-limited against the previous weight (see core/tuning.py)."""
    weights = dict(INDICATOR_WEIGHTS)
    prev = prev or {}
    # Anchor to the last written weights: an indicator whose sample count
    # drops below min_samples keeps its current tuned value instead of
    # snapping back to the code default (which would bypass the step cap).
    for name, val in prev.items():
        if name in weights:
            try:
                weights[name] = float(val)
            except (TypeError, ValueError):
                pass
    for name, s in stats.items():
        if s["n"] < min_samples:
            continue
        target = accuracy_to_weight(s["correct"], s["n"])
        anchor = weights.get(name, TUNED_WEIGHT_FLOOR)
        weights[name] = step_limited(target, anchor)
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
    all_rows = [r for d in dates for r in load_results(d)]
    rows, grading_schema = latest_grading_rows(all_rows)
    stats = indicator_stats(rows)
    if not stats:
        return None
    prev_all = _previous_weights()
    payload = {
        "generated": datetime.now(pytz.UTC).isoformat(),
        "days": len(dates),
        "date_range": [dates[0], dates[-1]],
        "min_samples": min_samples,
        "grading_schema": grading_schema,
        "excluded_older_schema_rows": len(all_rows) - len(rows),
        # Nested per-interval schema ({"15": {...}, "5": {...}}); the live
        # loader (strategy._weight_overrides) detects it and picks the set
        # matching the stream it's forecasting.
        "samples": {iv: {k: v["n"] for k, v in s.items()} for iv, s in stats.items()},
        "weights": {iv: suggest_weights(s, min_samples, _prev_for_interval(prev_all, iv))
                    for iv, s in stats.items()},
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

    all_rows = [r for d in dates for r in load_results(d)]
    rows, grading_schema = latest_grading_rows(all_rows)
    stats = indicator_stats(rows)
    if not stats:
        print(f"No graded indicator signals in {len(dates)} result day(s) "
              "(latest grading schema and interval-labeled rows only).")
        return

    prev_all = _previous_weights()
    # Naive baselines: an indicator below these is worse than doing nothing.
    from research.core.analytics import indicator_overlap, stream_baselines
    baselines = {b["stream"]: b for b in stream_baselines(rows)}
    flagged_overlap = [o for o in indicator_overlap(rows) if o["flagged"]]

    excluded = len(all_rows) - len(rows)
    print(f"Indicator accuracy over {len(dates)} day(s) ({dates[0]} to {dates[-1]}), "
          f"grading schema {grading_schema} ({excluded} older-schema rows excluded):")
    for interval in sorted(stats, key=lambda k: -int(k)):
        s_int = stats[interval]
        prev = _prev_for_interval(prev_all, interval)
        suggested = suggest_weights(s_int, args.min_samples, prev)
        print(f"\n[{interval}-min stream]")
        b = baselines.get(f"{interval}m")
        if b:
            print(f"baselines: always-BULLISH {b['always_bullish']:.1f}% (n={b['n']}) | "
                  f"persistence {b['persistence']:.1f}% (n={b['persistence_n']})")
        print(f"{'indicator':<12} {'graded':>6} {'accuracy':>9} {'shrunk':>8} {'prev':>6} {'suggested':>10}")
        for name in sorted(s_int, key=lambda k: -(s_int[k]['correct'] / s_int[k]['n'])):
            s = s_int[name]
            acc = s["correct"] / s["n"]
            shrunk = shrunk_accuracy(s["correct"], s["n"])
            cur = float(prev.get(name, INDICATOR_WEIGHTS.get(name, 0.0)))
            new = suggested.get(name, cur)
            note = "  (kept -- too few samples)" if s["n"] < args.min_samples else ""
            print(f"{name:<12} {s['n']:>6} {acc:>8.1%} {shrunk:>7.1%} {cur:>6.1f} {new:>10.1f}{note}")

    if flagged_overlap:
        print("\nNear-duplicate voters (>= 85% vote agreement — one signal counted twice;"
              "\nconsider dropping/merging the weaker of each pair):")
        for o in flagged_overlap:
            print(f"  [{o['stream']}] {o['pair']}: {o['agreement']:.1f}% over {o['n']} windows")

    if args.write:
        retune(args.days, args.min_samples, write=True)
        print(f"\nWrote {WEIGHTS_PATH} -- the bot will pick it up on its next forecast.")
    else:
        print("\nDry run -- pass --write to save data/indicator_weights.json.")


if __name__ == "__main__":
    main()
