"""Applicability check for the Dow Theory reversal rule (core.indicators.dow_trend).

    python backtest_dow.py --selftest                    # offline logic check, no network
    python backtest_dow.py                               # swing tickers + SPY/QQQ, daily, from 2021
    python backtest_dow.py --tickers SPY --interval 5m   # 0DTE frame (yfinance: last ~60 days only)
    python backtest_dow.py --verbose                     # list every reversal event

Bulk-downloads candles once per ticker via yfinance (no API budget) and
replays the walk-forward detector, grading two things:

  1. EVENT accuracy — after each reversal event, did price move in the called
     direction over the next N bars (deadband-graded, like live tuning)?
  2. STATE accuracy — treating the standing Dow trend as a regime vote on
     every bar, same grading. This is what a persistent "DowTrend" voter
     would score; events are what a recency-gated "DowReversal" voter scores.

On the daily frame it also reports overlap with the existing swing Structure
voter at each event bar — if Structure already agrees whenever the rule
fires, a new voter adds weight but no information.

Decision rule before wiring a voter into swing.strategy.evaluate_daily (or
the 0DTE forecast): event accuracy should beat both ~50% and the Structure
voter's accuracy on the same period (see backtest_swing.py's per-indicator
table), on a sample big enough to mean something (>50 graded events).
"""

from __future__ import annotations

import argparse
import sys
import os

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

from collections import defaultdict

import numpy as np
import pandas as pd

import config as cfg
from core.indicators import dow_trend, pivots

DEADBAND_DAILY = 0.5      # % — same as swing tracker grading
DEADBAND_INTRADAY = 0.1   # % — a 0.5% band would grade almost no 5m bars
HORIZONS_DAILY = (5, 10)      # bars ahead to grade (1-2 weeks, swing horizon)
HORIZONS_INTRADAY = (6, 12)   # 30/60 min on 5m bars, matching forecast windows


def load_candles(ticker: str, start: str, interval: str) -> pd.DataFrame | None:
    import yfinance as yf
    if interval == "1d":
        df = yf.Ticker(ticker).history(start=start, interval="1d", auto_adjust=True)
    else:
        # yfinance caps intraday history (~60 days for 5m/15m).
        df = yf.Ticker(ticker).history(period="60d", interval=interval, auto_adjust=True)
    if df is None or df.empty:
        return None
    df = df.reset_index()
    df = df.rename(columns={c: c.lower() for c in df.columns})
    ts_col = "date" if "date" in df.columns else "datetime"
    df = df.rename(columns={ts_col: "timestamp"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def structure_vote(closes: np.ndarray, i: int) -> str:
    """The existing swing Structure voter, replayed at bar i (last 40 closes)."""
    window = closes[max(0, i - 39):i + 1]
    p_highs, p_lows = pivots(window, 2)
    if len(p_highs) >= 2 and len(p_lows) >= 2:
        hh = window[p_highs[-1]] > window[p_highs[-2]]
        hl = window[p_lows[-1]] > window[p_lows[-2]]
        if hh and hl:
            return "BULLISH"
        if not hh and not hl:
            return "BEARISH"
    return "NEUTRAL"


def grade(direction: str, fwd_pct: float, deadband: float) -> bool | None:
    """True/False = correct/wrong; None = move inside the deadband (ungraded)."""
    if fwd_pct >= deadband:
        return direction == "BULLISH"
    if fwd_pct <= -deadband:
        return direction == "BEARISH"
    return None


def run(tickers: list[str], start: str, interval: str, order: int,
        verbose: bool) -> None:
    daily = interval == "1d"
    deadband = DEADBAND_DAILY if daily else DEADBAND_INTRADAY
    horizons = HORIZONS_DAILY if daily else HORIZONS_INTRADAY

    ev = {h: defaultdict(lambda: [0, 0, []]) for h in horizons}  # h -> dir -> [wins, graded, signed moves]
    state_acc = [0, 0]
    overlap = defaultdict(int)
    per_ticker = defaultdict(lambda: [0, 0])
    all_events = []

    for tkr in tickers:
        df = load_candles(tkr, start, interval)
        if df is None or len(df) < 60:
            print(f"{tkr}: not enough data — skipped")
            continue
        highs = df["high"].to_numpy(float)
        lows = df["low"].to_numpy(float)
        closes = df["close"].to_numpy(float)
        stamps = pd.to_datetime(df["timestamp"])

        states, events = dow_trend(highs, lows, closes, order)

        for i, direction in events:
            if daily:
                overlap["Structure agrees" if structure_vote(closes, i) == direction
                        else "Structure neutral" if structure_vote(closes, i) == "NEUTRAL"
                        else "Structure disagrees"] += 1
            for h in horizons:
                if i + h >= len(closes):
                    continue
                fwd = (closes[i + h] / closes[i] - 1) * 100
                g = grade(direction, fwd, deadband)
                ev[h][direction][2].append(fwd if direction == "BULLISH" else -fwd)
                if g is not None:
                    ev[h][direction][0] += g
                    ev[h][direction][1] += 1
                    if h == horizons[-1]:
                        per_ticker[tkr][0] += g
                        per_ticker[tkr][1] += 1
            all_events.append((tkr, stamps.iloc[i], direction))

        h_state = horizons[-1]
        for i, st in enumerate(states):
            if st is None or i + h_state >= len(closes):
                continue
            g = grade("BULLISH" if st == "UP" else "BEARISH",
                      (closes[i + h_state] / closes[i] - 1) * 100, deadband)
            if g is not None:
                state_acc[0] += g
                state_acc[1] += 1

        span_days = max((stamps.iloc[-1] - stamps.iloc[0]).days, 1)
        print(f"{tkr}: {len(df)} bars, {len(events)} reversal events "
              f"({len(events) / (span_days / 365.25):.1f}/yr)")

    if not all_events:
        print("No reversal events found.")
        return

    print(f"\n=== Dow reversal EVENT accuracy (deadband ±{deadband}%) ===")
    for h in horizons:
        wins = sum(s[0] for s in ev[h].values())
        n = sum(s[1] for s in ev[h].values())
        moves = [m for s in ev[h].values() for m in s[2]]
        if not n:
            print(f"+{h} bars: no graded events")
            continue
        print(f"+{h} bars: {wins}/{n} correct ({wins / n * 100:.1f}%) | "
              f"avg signed move {np.mean(moves):+.2f}% | median {np.median(moves):+.2f}%")
        for d, s in sorted(ev[h].items()):
            if s[1]:
                print(f"    {d:7s} {s[0]}/{s[1]} ({s[0] / s[1] * 100:.0f}%), "
                      f"avg {np.mean(s[2]):+.2f}%")

    if state_acc[1]:
        print(f"\nState-as-regime accuracy (every bar, +{horizons[-1]} bars): "
              f"{state_acc[0]}/{state_acc[1]} ({state_acc[0] / state_acc[1] * 100:.1f}%)")
    if daily and overlap:
        total = sum(overlap.values())
        print("Overlap with swing Structure voter at event bars: "
              + " | ".join(f"{k}: {v}/{total}" for k, v in sorted(overlap.items())))
    graded = {t: (w, n) for t, (w, n) in per_ticker.items() if n}
    if graded:
        print("Per ticker (+%d bars): " % horizons[-1] + " | ".join(
            f"{t}: {w}/{n} ({w / n * 100:.0f}%)" for t, (w, n) in sorted(graded.items())))

    if verbose:
        print("\nAll events:")
        for tkr, ts, direction in all_events:
            print(f"  {ts} {tkr:5s} {direction}")


def selftest() -> None:
    """Deterministic logic check — no network, no randomness.

    Builds a zig-zag that makes higher highs/lows, then prints a lower high
    and breaks the last swing low (and the mirror), and asserts the detector
    fires exactly there, walk-forward.
    """
    def make(path: list[float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        closes = np.array(path, dtype=float)
        return closes + 0.4, closes - 0.4, closes

    # Uptrend: swings 100->105->103->108->106->111, then lower high 109,
    # then a break below the last swing low (106).
    up_then_break = [100, 102, 105, 104, 103, 105, 108, 107, 106, 108, 111,
                     110, 109, 108, 109, 108, 107, 105.4, 104, 103]
    highs, lows, closes = make(up_then_break)
    states, events = dow_trend(highs, lows, closes, order=2)
    assert any(s == "UP" for s in states), "uptrend never established"
    assert len(events) == 1 and events[0][1] == "BEARISH", f"expected 1 BEARISH event, got {events}"
    i = events[0][0]
    assert states[i] == "DOWN" and states[i - 1] == "UP", "state did not flip at the event bar"

    # Mirror: downtrend, higher low, then a close above the last swing high.
    down_then_break = [120, 118, 115, 116, 117, 115, 112, 113, 114, 112, 109,
                       110, 111, 112, 111, 112, 113, 114.6, 116, 117]
    highs, lows, closes = make(down_then_break)
    states, events = dow_trend(highs, lows, closes, order=2)
    assert len(events) == 1 and events[0][1] == "BULLISH", f"expected 1 BULLISH event, got {events}"

    # No lookahead: truncating the series at the event bar reproduces the event.
    highs, lows, closes = make(up_then_break)
    _, full_events = dow_trend(highs, lows, closes, order=2)
    i = full_events[0][0]
    _, cut_events = dow_trend(highs[:i + 1], lows[:i + 1], closes[:i + 1], order=2)
    assert cut_events and cut_events[-1] == full_events[0], "event depends on future bars"

    # A trend that never breaks structure fires nothing.
    steady = [100 + 0.5 * k + (1.5 if k % 4 < 2 else 0) for k in range(30)]
    highs, lows, closes = make(steady)
    _, events = dow_trend(highs, lows, closes, order=2)
    assert events == [], f"steady uptrend fired spurious events: {events}"

    print("selftest: PASS (4 scenarios)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Check the Dow reversal rule's applicability")
    ap.add_argument("--tickers", default=",".join(cfg.SWING_TICKERS + ["SPY", "QQQ"]))
    ap.add_argument("--start", default="2021-01-01", help="daily interval only")
    ap.add_argument("--interval", default="1d", choices=("1d", "15m", "5m"))
    ap.add_argument("--order", type=int, default=2, help="pivot confirmation window")
    ap.add_argument("--selftest", action="store_true", help="offline detector logic check")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    run(tickers, args.start, args.interval, args.order, args.verbose)


if __name__ == "__main__":
    main()
