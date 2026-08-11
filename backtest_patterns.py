"""Backtest daily candlestick patterns as a next-day direction signal.

    python backtest_patterns.py                          # SPY+QQQ, 10 years
    python backtest_patterns.py --tickers SPY --years 5
    python backtest_patterns.py --verbose                # print every hit

Bulk-downloads daily candles once per ticker via yfinance (no API budget) and
replays core.indicators.detect_candlestick_patterns as-of each day: patterns
are computed from candles strictly BEFORE the graded day (`iloc[:i]` — the
strategy log's lookahead lesson: `<=` on the decision day hands the harness
the label it is predicting) and graded against that day's close vs open, the
same rule zero_dte.tracker.evaluate_bias uses. Whatever this script measures
is therefore exactly what a 9:35 AM live read of yesterday's candles can know.

Per directional pattern it reports: n, next-day accuracy, the always-BULLISH
baseline over the SAME days that pattern fired (a pattern must beat the drift
of its own days, not the whole tape), average signed next-day move, and the
with-trend / counter-trend split vs the 50 SMA. Indecision patterns (Doji,
Inside Bar) are graded as chop predictions instead: is the next day's |move|
below the trailing-20-day median |move|?

Purely offline analysis — nothing here changes live behavior. Adoption
decision (voter / replacement / rejection) goes through docs/strategy-log.md.
"""

from __future__ import annotations

import argparse
import sys
import os

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

from collections import defaultdict

import pandas as pd

from core.indicators import detect_candlestick_patterns, sma

WARMUP = 60          # bars before the first graded day (SMA50 context + slack)
DETECT_TAIL = 30     # candles handed to the detector (needs lookback+3)
CHOP_WINDOW = 20     # trailing window for the median |move| chop baseline


def load_daily(ticker: str, years: int) -> pd.DataFrame | None:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if df is None or df.empty:
        return None
    df = df.reset_index().rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def replay(df: pd.DataFrame, verbose: bool = False,
           ticker: str = "?") -> dict[str, dict]:
    """As-of replay over one ticker's daily frame. Returns per-pattern stats."""
    dates = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.date
    closes = df["close"].astype(float)
    sma50 = sma(closes, 50)
    abs_move = (closes / df["open"].astype(float) - 1.0).abs() * 100

    stats: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "correct": 0, "moves": [],
        "with_trend": [0, 0], "counter_trend": [0, 0],   # [correct, n]
        "chop_correct": 0,
    })
    # Unconditional drift over the same graded days — the honest baseline.
    # P(up | pattern fired) must beat P(up) on any day, not 50%: on an index
    # that drifts up ~54% of days, a bullish pattern at 54% knows nothing.
    days = bull_days = 0

    for i in range(WARMUP, len(df)):
        day = df.iloc[i]
        o, c = float(day["open"]), float(day["close"])
        actual = "BULLISH" if c > o else "BEARISH"
        move_pct = (c / o - 1.0) * 100
        days += 1
        bull_days += actual == "BULLISH"

        window = df.iloc[:i]                      # strictly before the graded day
        hits = detect_candlestick_patterns(window.tail(DETECT_TAIL))
        if not hits:
            continue
        # Trend as of the same information set: yesterday's close vs SMA50.
        trend_up = closes.iloc[i - 1] > sma50.iloc[i - 1] if not pd.isna(sma50.iloc[i - 1]) else None
        chop_ref = abs_move.iloc[max(0, i - CHOP_WINDOW):i].median()

        for h in hits:
            s = stats[h["name"]]
            s["n"] += 1
            if h["direction"] is None:
                if abs_move.iloc[i] < chop_ref:
                    s["chop_correct"] += 1
                continue
            correct = h["direction"] == actual
            s["correct"] += correct
            s["moves"].append(move_pct if h["direction"] == "BULLISH" else -move_pct)
            if trend_up is not None:
                aligned = (h["direction"] == "BULLISH") == trend_up
                bucket = s["with_trend"] if aligned else s["counter_trend"]
                bucket[0] += correct
                bucket[1] += 1
            if verbose:
                print(f"  {dates.iloc[i]}  {ticker:4s} {h['name']:18s} "
                      f"{h['direction']:7s} -> {actual:7s} "
                      f"({'HIT ' if correct else 'MISS'} {move_pct:+.2f}%)")
    stats["_meta"] = {"days": days, "bull_days": bull_days}
    return stats


def merge(into: dict, other: dict) -> None:
    for name, s in other.items():
        if name == "_meta":
            m = into.setdefault("_meta", {"days": 0, "bull_days": 0})
            m["days"] += s["days"]
            m["bull_days"] += s["bull_days"]
            continue
        t = into[name]
        t["n"] += s["n"]
        t["correct"] += s["correct"]
        t["moves"].extend(s["moves"])
        t["chop_correct"] += s["chop_correct"]
        for key in ("with_trend", "counter_trend"):
            t[key][0] += s[key][0]
            t[key][1] += s[key][1]


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "    —"


def report(stats: dict[str, dict]) -> None:
    meta = stats.get("_meta", {"days": 0, "bull_days": 0})
    drift_bull = meta["bull_days"] / meta["days"] if meta["days"] else 0.5
    patterns = {k: v for k, v in stats.items() if k != "_meta"}
    directional = {k: v for k, v in patterns.items() if v["moves"] or v["correct"]}
    indecision = {k: v for k, v in patterns.items() if k not in directional}

    if meta["days"]:
        print(f"\nGraded days: {meta['days']}  |  up-day drift: {100 * drift_bull:.1f}% "
              f"(the bar a bullish pattern must beat; bearish must beat {100 * (1 - drift_bull):.1f}%)")

    if directional:
        print(f"\n{'Pattern':18s} {'n':>5s} {'acc':>6s} {'drift':>6s} {'edge':>6s} "
              f"{'avg mv':>7s} {'w/trend':>8s} {'counter':>8s}")
        dir_n = sum(v["n"] for v in directional.values())
        for name, s in sorted(directional.items(), key=lambda kv: -kv[1]["n"]):
            avg_mv = sum(s["moves"]) / len(s["moves"]) if s["moves"] else 0.0
            # The honest baseline: the unconditional rate of this pattern's
            # direction. P(up|fired) at the drift rate = the pattern knows
            # nothing; only the margin above drift is information.
            base = drift_bull if _dir_of(name) == "BULLISH" else 1 - drift_bull
            edge = 100 * (s["correct"] / s["n"] - base) if s["n"] else 0.0
            print(f"{name:18s} {s['n']:5d} {_pct(s['correct'], s['n']):>6s} "
                  f"{100 * base:5.1f}% {edge:+5.1f}p {avg_mv:+6.2f}% "
                  f"{_pct(*s['with_trend']):>8s} {_pct(*s['counter_trend']):>8s}")
        print(f"{'ALL directional':18s} {dir_n:5d} "
              f"{_pct(sum(v['correct'] for v in directional.values()), dir_n):>6s}")
        print("\n  edge = accuracy minus the unconditional drift of the pattern's own")
        print("  direction. A bullish pattern at the up-day drift rate knows nothing.")

    if indecision:
        print(f"\n{'Indecision':18s} {'n':>5s} {'chop%':>6s}   (next-day |move| < trailing median)")
        for name, s in sorted(indecision.items(), key=lambda kv: -kv[1]["n"]):
            print(f"{name:18s} {s['n']:5d} {_pct(s['chop_correct'], s['n']):>6s}")
        print("  ~50% = no information; materially above = usable chop flag "
              "(FORECAST deadband / VOLATILE context)")


def _dir_of(name: str) -> str:
    return "BULLISH" if name in ("Hammer", "Morning Star", "Bullish Engulfing",
                                 "Bullish Marubozu") else "BEARISH"


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest daily candlestick patterns")
    ap.add_argument("--tickers", default="SPY,QQQ")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    total: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "correct": 0, "moves": [],
        "with_trend": [0, 0], "counter_trend": [0, 0], "chop_correct": 0})

    for ticker in [t.strip().upper() for t in args.tickers.split(",") if t.strip()]:
        df = load_daily(ticker, args.years)
        if df is None or len(df) < WARMUP + 10:
            print(f"[{ticker}] no/insufficient data — skipped")
            continue
        print(f"[{ticker}] {len(df)} daily candles "
              f"({pd.to_datetime(df['timestamp'].iloc[0]).date()} -> "
              f"{pd.to_datetime(df['timestamp'].iloc[-1]).date()})")
        merge(total, replay(df, verbose=args.verbose, ticker=ticker))

    if not total:
        print("Nothing to report.")
        return
    report(total)


if __name__ == "__main__":
    main()
