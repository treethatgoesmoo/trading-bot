"""Retune the swing indicator weights from graded live results.

Reads data/swing_outcomes-*.jsonl (written by the pre-open grading pass),
measures each indicator's directional accuracy against the actual move over
the signal horizon, and maps accuracy to a weight:

    weight = (shrunk_accuracy - 0.50) * 20, floored at TUNED_WEIGHT_FLOOR

where shrunk_accuracy pulls the measured rate toward 50% by
TUNED_WEIGHT_SHRINKAGE coin-flip pseudo-samples and the result moves at most
TUNED_WEIGHT_MAX_STEP per retune (see core/tuning.py) — swing samples are
tiny, so raw rates off a handful of signals are mostly noise. The floor keeps
underperformers to a token vote instead of zero — a 0-weight dissenter
disappears from the confidence denominator, inflating confidence. Indicators
with fewer than --min-samples graded signals keep their current default
weight (not enough evidence to move them).

Usage:
    python -m research.tune_swing_weights               # report only
    python -m research.tune_swing_weights --days 60     # last 60 outcome days
    python -m research.tune_swing_weights --write       # historical weights only

The bot picks up data/swing_weights.json live (swing.strategy hot-reloads it
on file change) — no restart needed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime

import pytz

from research.config import TUNED_WEIGHT_FLOOR
from core.paths import SWING_WEIGHTS_FILE
from research.core.tracking import available_dates, load_jsonl
from research.core.tuning import accuracy_to_weight, load_previous_weights, shrunk_accuracy, step_limited
from research.swing.strategy import SWING_INDICATOR_WEIGHTS

ET = pytz.timezone("US/Eastern")
DIRECTIONAL = ("BULLISH", "BEARISH")


def indicator_stats(dates: list[str]) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0})
    for d in dates:
        for o in load_jsonl("swing_outcomes", d):
            actual = o.get("actual")
            if actual not in DIRECTIONAL:
                continue
            for name, signal in (o.get("signals") or {}).items():
                if signal in DIRECTIONAL:
                    agg[name]["n"] += 1
                    agg[name]["correct"] += (signal == actual)
    return dict(agg)


def _previous_weights() -> dict:
    return load_previous_weights(SWING_WEIGHTS_FILE)


def suggest_weights(stats: dict[str, dict], min_samples: int,
                    prev: dict | None = None) -> dict[str, float]:
    """Damped accuracy -> weight mapping: shrinkage toward 50% by sample
    size, then step-limited against the previous weight (see core/tuning.py).
    Swing samples are tiny (a handful of graded signals per indicator), so
    the shrinkage matters even more here than for the 0DTE streams."""
    weights = dict(SWING_INDICATOR_WEIGHTS)
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


def retune(days: int = 0, min_samples: int = 15, write: bool = False) -> dict | None:
    """Compute (and optionally persist) tuned swing weights from graded
    outcomes. Returns the payload dict, or None with no graded history yet.
    Called by the CLI below and by the bot's nightly housekeeping cycle.
    """
    dates = available_dates("swing_outcomes")
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
        "samples": {k: v["n"] for k, v in stats.items()},
        "weights": suggest_weights(stats, min_samples, _previous_weights()),
    }
    if write:
        tmp = SWING_WEIGHTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, SWING_WEIGHTS_FILE)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=0,
                    help="use only the last N outcome days (default: all)")
    ap.add_argument("--min-samples", type=int, default=15,
                    help="graded signals required before an indicator's weight moves (default: 15)")
    ap.add_argument("--write", action="store_true",
                    help="save the suggested weights to data/swing_weights.json")
    args = ap.parse_args()

    dates = available_dates("swing_outcomes")
    if args.days > 0:
        dates = dates[-args.days:]
    if not dates:
        print("No swing_outcomes-*.jsonl files found -- signals grade after their 1-2 week horizon.")
        return

    stats = indicator_stats(dates)
    if not stats:
        print(f"No graded indicator signals in {len(dates)} outcome day(s).")
        return

    prev = _previous_weights()
    suggested = suggest_weights(stats, args.min_samples, prev)

    print(f"Swing indicator accuracy over {len(dates)} day(s) ({dates[0]} to {dates[-1]}):\n")
    print(f"{'indicator':<14} {'graded':>6} {'accuracy':>9} {'shrunk':>8} {'prev':>6} {'suggested':>10}")
    for name in sorted(stats, key=lambda k: -(stats[k]['correct'] / stats[k]['n'])):
        s = stats[name]
        acc = s["correct"] / s["n"]
        shrunk = shrunk_accuracy(s["correct"], s["n"])
        cur = float(prev.get(name, SWING_INDICATOR_WEIGHTS.get(name, 0.0)))
        new = suggested.get(name, cur)
        note = "  (kept -- too few samples)" if s["n"] < args.min_samples else ""
        print(f"{name:<14} {s['n']:>6} {acc:>8.1%} {shrunk:>7.1%} {cur:>6.1f} {new:>10.1f}{note}")

    if args.write:
        retune(args.days, args.min_samples, write=True)
        print(f"\nWrote {SWING_WEIGHTS_FILE} -- the bot will pick it up on its next scan.")
    else:
        print("\nDry run -- pass --write to save data/swing_weights.json.")


if __name__ == "__main__":
    main()
