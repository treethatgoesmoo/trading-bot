"""Retune the swing indicator weights from graded live results.

Reads data/swing_outcomes-*.jsonl (written by the pre-open grading pass),
measures each indicator's directional accuracy against the actual move over
the signal horizon, and maps accuracy to a weight:

    weight = (accuracy - 0.50) * 20, floored at 0

so 55% -> 1.0, 60% -> 2.0, 65% -> 3.0, and anything at or below coin-flip gets
no vote. Indicators with fewer than --min-samples graded signals keep their
current default weight (not enough evidence to move them).

Usage:
    python tune_swing_weights.py               # report only
    python tune_swing_weights.py --days 60     # limit to the last 60 outcome days
    python tune_swing_weights.py --write       # save data/swing_weights.json

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

from core.paths import SWING_WEIGHTS_FILE
from core.tracking import available_dates, load_jsonl
from swing.strategy import SWING_INDICATOR_WEIGHTS

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


def suggest_weights(stats: dict[str, dict], min_samples: int) -> dict[str, float]:
    weights = dict(SWING_INDICATOR_WEIGHTS)
    for name, s in stats.items():
        if s["n"] < min_samples:
            continue
        acc = s["correct"] / s["n"]
        weights[name] = round(max((acc - 0.50) * 20, 0.0), 1)
    return weights


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

    suggested = suggest_weights(stats, args.min_samples)

    print(f"Swing indicator accuracy over {len(dates)} day(s) ({dates[0]} to {dates[-1]}):\n")
    print(f"{'indicator':<14} {'graded':>6} {'accuracy':>9} {'current':>8} {'suggested':>10}")
    for name in sorted(stats, key=lambda k: -(stats[k]['correct'] / stats[k]['n'])):
        s = stats[name]
        acc = s["correct"] / s["n"]
        cur = SWING_INDICATOR_WEIGHTS.get(name, 0.0)
        new = suggested.get(name, cur)
        note = "  (kept -- too few samples)" if s["n"] < args.min_samples else ""
        print(f"{name:<14} {s['n']:>6} {acc:>8.1%} {cur:>8.1f} {new:>10.1f}{note}")

    if args.write:
        payload = {
            "generated": datetime.now(ET).isoformat(),
            "days": len(dates),
            "date_range": [dates[0], dates[-1]],
            "min_samples": args.min_samples,
            "samples": {k: v["n"] for k, v in stats.items()},
            "weights": suggested,
        }
        tmp = SWING_WEIGHTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, SWING_WEIGHTS_FILE)
        print(f"\nWrote {SWING_WEIGHTS_FILE} -- the bot will pick it up on its next scan.")
    else:
        print("\nDry run -- pass --write to save data/swing_weights.json.")


if __name__ == "__main__":
    main()
