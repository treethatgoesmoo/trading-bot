"""
Session-third breakdown of the 15m/5m forecast ensemble's graded history.

Motivation: docs/state-of-the-code.md already shows the 15m stream beating
its always-BULLISH baseline pooled across the whole day (RSI 61.3%, VWAP
58.9%, MACD 57.6%, Momentum 56.3%). Intraday-momentum literature says signal
quality is NOT uniform across the session (U-shaped volatility/volume; the
first-30-min -> last-30-min effect is a specific open/close relationship,
not "momentum persists all day"). This script re-slices the exact same
graded rows (data/results-*.jsonl via zero_dte.tracker) by session third —
Open (9:30-10:30 ET), Mid (10:30-14:30 ET), Close (14:30-16:00 ET) — so we
can see whether the pooled edge is real everywhere or concentrated/diluted
by time of day, per stream (5m vs 15m), reusing the same DIRECTIONAL/
always-BULLISH baseline definitions as core/analytics.py (no new grading
logic, read-only, no network).

Usage:
    python analyze_session_thirds.py                 # all history
    python analyze_session_thirds.py --days 20        # last 20 trading days
    python analyze_session_thirds.py --stream 15       # only the 15m stream
"""

import argparse
from collections import defaultdict
from datetime import datetime, time as dtime

import pytz

from core.analytics import latest_grading_rows
from zero_dte import tracker

ET = pytz.timezone("US/Eastern")
DIRECTIONAL = ("BULLISH", "BEARISH")

BUCKETS = [
    ("Open   9:30-10:30", dtime(9, 30), dtime(10, 30)),
    ("Mid   10:30-14:30", dtime(10, 30), dtime(14, 30)),
    ("Close 14:30-16:00", dtime(14, 30), dtime(16, 0)),
]


def bucket_for(ts_et: datetime) -> str | None:
    t = ts_et.time()
    for label, lo, hi in BUCKETS:
        if lo <= t < hi:
            return label
    return None


def load_all_results(days: int | None) -> list[dict]:
    dates = tracker.available_dates("results")
    if days is not None and days > 0:
        dates = dates[-days:]
    out = []
    for d in dates:
        for r in tracker.load_results(d):
            r["date"] = d
            out.append(r)
    return out


def pct(c: int, n: int) -> float:
    return round(c / n * 100, 1) if n else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="most recent N trading days (default: all)")
    ap.add_argument("--stream", type=str, default=None, help="restrict to one stream, e.g. 15 or 5")
    ap.add_argument("--min-n", type=int, default=15, help="hide indicator rows below this sample size")
    ap.add_argument("--grading-schema", default="latest",
                    help="latest (default), all, or an integer schema")
    args = ap.parse_args()

    all_results = load_all_results(args.days)
    if args.grading_schema == "latest":
        results, grading_schema = latest_grading_rows(all_results)
    elif args.grading_schema == "all":
        results, grading_schema = all_results, "all"
    else:
        grading_schema = int(args.grading_schema)
        results = [r for r in all_results
                   if int(r.get("grading_schema", 1)) == grading_schema]
    if args.stream is not None:
        want = int(args.stream)
        results = [r for r in results if r.get("interval") == want]

    # attach ET bucket to each row
    tagged = []
    for r in results:
        try:
            ts = datetime.fromisoformat(r["time"])
        except Exception:
            continue
        ts_et = ts.astimezone(ET)
        b = bucket_for(ts_et)
        if b is None:
            continue
        r["_bucket"] = b
        tagged.append(r)

    streams = sorted({r.get("interval") for r in tagged}, key=lambda x: (x is None, x))

    print(f"\n{'='*78}")
    print(f"  Session-third breakdown — {len(tagged)} graded rows across "
          f"{len(all_results)} total · grading schema {grading_schema}"
          f"{' (stream filter: ' + args.stream + 'm)' if args.stream else ''}")
    print(f"{'='*78}")

    for interval in streams:
        stream_label = f"{interval}m" if interval is not None else "mixed (no interval label)"
        stream_rows = [r for r in tagged if r.get("interval") == interval]

        print(f"\n--- Stream: {stream_label} ({len(stream_rows)} rows) ---")

        # Paired directional comparison: forecast and baseline must be scored
        # on exactly the same rows. Including VOLATILE-on-FLAT wins in one
        # side but not the other would make the reported edge meaningless.
        header = f"{'Bucket':<20}{'n(paired)':>10}{'fc acc %':>10}{'always-BULL %':>15}{'edge (pp)':>11}"
        print(header)
        print("-" * len(header))
        for label, _, _ in BUCKETS:
            sub = [r for r in stream_rows if r["_bucket"] == label]
            paired = [
                r for r in sub
                if r.get("direction") in DIRECTIONAL
                and r.get("actual") in DIRECTIONAL
            ]
            correct = sum(1 for r in paired if r["direction"] == r["actual"])
            bull_actuals = sum(1 for r in paired if r["actual"] == "BULLISH")
            fc_acc = pct(correct, len(paired))
            base = pct(bull_actuals, len(paired))
            edge = round(fc_acc - base, 1) if paired else None
            print(f"{label:<20}{len(paired):>10}{fc_acc:>10}{base:>15}"
                  f"{(f'{edge:+.1f}' if edge is not None else '-'):>11}")

        # Per-indicator accuracy, per bucket (only indicators with enough n)
        agg = defaultdict(lambda: defaultdict(lambda: {"n": 0, "correct": 0}))
        for r in stream_rows:
            actual = r.get("actual")
            if actual not in DIRECTIONAL:
                continue
            for name, signal in (r.get("signals") or {}).items():
                if signal in DIRECTIONAL:
                    a = agg[name][r["_bucket"]]
                    a["n"] += 1
                    a["correct"] += (signal == actual)

        indicator_names = sorted(agg.keys())
        if indicator_names:
            print(f"\n  Per-indicator accuracy by bucket (n<{args.min_n} shown as '-'):")
            col_labels = [b[0].split()[0] for b in BUCKETS]
            hdr2 = f"  {'Indicator':<12}" + "".join(f"{c + ' (n/acc%)':>22}" for c in col_labels)
            print(hdr2)
            print("  " + "-" * (len(hdr2) - 2))
            for name in indicator_names:
                row = f"  {name:<12}"
                for label, _, _ in BUCKETS:
                    a = agg[name][label]
                    if a["n"] < args.min_n:
                        cell = "-"
                    else:
                        cell = f"{a['n']}/{pct(a['correct'], a['n'])}%"
                    row += f"{cell:>22}"
                print(row)

    print(f"\n{'='*78}")
    print("  Read: both columns use the exact same rows: directional forecast and")
    print("  directional actual. 'always-BULL %' is the paired drift baseline.")
    print("  A bucket where fc acc doesn't clear the baseline is not adding")
    print("  information in that part of the session even if the pooled number does.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
