"""Audit the break-and-retest filter stack against recorded history.

Replays every logged setup — fired AND filtered — from data/filters-*.jsonl as
if it had been taken: entry at the signal's 5-min bar close, stop at the
opposite side of that day's first 5-min candle (the live rule), target at
RISK_REWARD_RATIO x risk, tracked bar-by-bar to the session's last 5-min bar.
Both-touched-in-one-bar counts as a loss (conservative).

Then reports, per filter, the R its vetoes prevented (positive = the filter
saved money, negative = it vetoed winners) — the meta-labeling-style audit
for a veto stack that blocks most setups.

Uses bulk yfinance 5m history (~60-day lookback max) — no API budget. Setups
older than the lookback are skipped and counted.

Usage:
    python -m research.backtest_filters             # historical records
    python -m research.backtest_filters --verbose   # per-setup lines
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from datetime import datetime

import pandas as pd
import pytz
import yfinance as yf

from research.config import RISK_REWARD_RATIO
from core.paths import DATA_DIR

ET = pytz.timezone("US/Eastern")


def _blocking_failures(setup: dict) -> list[str]:
    """Names of the filters that actually vetoed this setup.

    Advisory filters are computed and logged but never block, so they must not
    be credited (or blamed) for a veto — otherwise an advisory filter riding
    along on someone else's veto inflates its own record and destroys the
    sole-veto counts. Records written before the advisory flag existed have no
    such key and behave exactly as they always did.
    """
    return [f["name"] for f in setup.get("filters", [])
            if not f.get("passed") and not f.get("advisory")]


def load_setups() -> list[dict]:
    setups = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "filters-*.jsonl"))):
        date = os.path.basename(path)[len("filters-"):-len(".jsonl")]
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                e["date"] = date
                setups.append(e)
    return setups


def fetch_5m(ticker: str) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(period="60d", interval="5m")
    except Exception as exc:
        print(f"  {ticker}: 5m fetch failed: {exc}")
        return None
    if df is None or df.empty:
        return None
    idx = df.index.tz_convert(ET) if df.index.tz is not None else df.index.tz_localize(ET)
    df = df.copy()
    df.index = idx
    return df


def simulate(day: pd.DataFrame, signal_time_et: datetime, direction: str,
             ) -> tuple[float, str, float, float] | None:
    """Replay one setup on its day's 5m bars.

    Returns (r_multiple, exit_kind, entry, risk) or None if it can't be replayed.
    """
    first = day.between_time("09:30", "09:34")
    if first.empty:
        return None
    box_high = float(first.iloc[0]["High"])
    box_low = float(first.iloc[0]["Low"])

    # Entry = close of the 5m bar containing the signal (bars are labeled by
    # their start time).
    entry_rows = day[day.index <= signal_time_et]
    if entry_rows.empty:
        return None
    entry_bar_ts = entry_rows.index[-1]
    entry = float(entry_rows.iloc[-1]["Close"])

    if direction == "LONG":
        stop, sign = box_low, 1.0
    else:
        stop, sign = box_high, -1.0
    risk = (entry - stop) * sign
    if risk <= 0:
        return None
    target = entry + sign * risk * RISK_REWARD_RATIO

    for ts, bar in day[day.index > entry_bar_ts].iterrows():
        hi, lo = float(bar["High"]), float(bar["Low"])
        stop_hit = lo <= stop if direction == "LONG" else hi >= stop
        target_hit = hi >= target if direction == "LONG" else lo <= target
        if stop_hit:  # conservative: stop wins ties within a bar
            return -1.0, "STOP", entry, risk
        if target_hit:
            return RISK_REWARD_RATIO, "TARGET", entry, risk
    last_close = float(day.iloc[-1]["Close"])
    return (last_close - entry) * sign / risk, "EOD", entry, risk


def fmt_stats(rs: list[float]) -> str:
    if not rs:
        return "n=0"
    wins = sum(1 for r in rs if r > 0)
    return (f"n={len(rs):<3} win={wins}/{len(rs)} ({wins / len(rs) * 100:3.0f}%)  "
            f"avg={sum(rs) / len(rs):+.2f}R  total={sum(rs):+.1f}R")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="print each replayed setup")
    args = ap.parse_args()

    setups = load_setups()
    if not setups:
        print("No filters-*.jsonl history found.")
        return
    print(f"{len(setups)} setups on record "
          f"({sum(1 for s in setups if s.get('fired'))} fired, "
          f"{sum(1 for s in setups if not s.get('fired'))} filtered)\n")

    frames: dict[str, pd.DataFrame | None] = {}
    replayed, skipped = [], 0
    for s in setups:
        ticker = s.get("ticker")
        if ticker not in frames:
            frames[ticker] = fetch_5m(ticker)
        df = frames[ticker]
        if df is None:
            skipped += 1
            continue
        t_et = datetime.fromisoformat(s["time"]).astimezone(ET)
        day = df[df.index.date == t_et.date()]
        result = simulate(day, t_et, s.get("direction", "?")) if not day.empty else None
        if result is None:
            skipped += 1
            continue
        r, kind, entry, risk = result
        s["_r"], s["_exit"] = r, kind
        replayed.append(s)
        if args.verbose:
            failed = _blocking_failures(s)
            tag = "FIRED " if s.get("fired") else f"VETOED[{','.join(failed) or '-'}]"
            print(f"  {s['date']} {ticker:5s} {s.get('direction', '?'):5s} {tag:28s} "
                  f"entry ${entry:.2f} risk ${risk:.2f} -> {kind:6s} {r:+.2f}R")
    if args.verbose:
        print()
    print(f"Replayed {len(replayed)} setups ({skipped} outside data window / no data)\n")
    if not replayed:
        return

    fired = [s["_r"] for s in replayed if s.get("fired")]
    vetoed = [s["_r"] for s in replayed if not s.get("fired")]
    everything = [s["_r"] for s in replayed]
    print(f"{'fired (taken live)':22s} {fmt_stats(fired)}")
    print(f"{'vetoed (would-be)':22s} {fmt_stats(vetoed)}")
    print(f"{'no filters at all':22s} {fmt_stats(everything)}\n")

    any_veto: dict[str, list[float]] = defaultdict(list)
    sole_veto: dict[str, list[float]] = defaultdict(list)
    for s in replayed:
        if s.get("fired"):
            continue
        failed = _blocking_failures(s)
        for name in failed:
            any_veto[name].append(s["_r"])
        if len(failed) == 1:
            sole_veto[failed[0]].append(s["_r"])

    print("Per-filter veto audit (what its vetoes would have returned;")
    print("negative avg R = the filter is earning its keep):")
    print(f"  {'filter':10s} | {'all vetoes it joined':44s} | sole vetoes")
    for name in sorted(any_veto, key=lambda n: -len(any_veto[n])):
        sole = fmt_stats(sole_veto.get(name, []))
        print(f"  {name:10s} | {fmt_stats(any_veto[name]):44s} | {sole}")

    print("\nNote: entries/exits approximated on 5m bars (live signals use 1m);")
    print("same-bar stop+target resolves as a loss. Read avg R, not pennies.")


if __name__ == "__main__":
    main()
