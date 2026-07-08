"""Backtest the swing options strategy over daily history.

    python backtest_swing.py                                   # config tickers, from 2023-01-01
    python backtest_swing.py --tickers TSLA,AMD --start 2024-01-01
    python backtest_swing.py --threshold 5.0 --verbose

Bulk-downloads daily candles once per ticker via yfinance (no API budget),
then replays the EXACT live logic — swing.strategy.evaluate_daily on a rolling
300-bar window, one evaluation per trading day — and grades each signal with
the same walk-forward rule the live grader uses (target-before-stop within the
horizon, stop wins same-day ties).

Reports win rate, resolution mix, per-indicator accuracy, and a threshold
sweep so SWING_SCORE_THRESHOLD / SWING_MIN_CONFIDENCE can be justified from
data before going live. Contracts aren't simulated (no historical chains) —
the underlying move is the graded quantity, matching swing_outcomes grading.
"""

from __future__ import annotations

import argparse
import sys
import os

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

from collections import defaultdict
from datetime import timedelta

import pandas as pd
import yfinance as yf

import config as cfg
from swing.strategy import MIN_BARS, evaluate_daily

WINDOW = 300  # mirrors the live fetch_candles(ticker, "D", 300)
DEADBAND = 0.5  # % — same as tracker.INDICATOR_DEADBAND_PCT


def load_daily(ticker: str, start: str) -> pd.DataFrame | None:
    # Pad the start so the first evaluated day already has a full window.
    padded = (pd.Timestamp(start) - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker).history(start=padded, interval="1d", auto_adjust=True)
    if df is None or df.empty:
        return None
    df = df.reset_index().rename(columns={
        "Date": "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume"})
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def evaluate_all(df: pd.DataFrame, start: str) -> list[dict]:
    """One vote per trading day (window ending that day), from `start` on."""
    votes = []
    dates = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.date
    start_date = pd.Timestamp(start).date()
    for i in range(MIN_BARS - 1, len(df) - 1):  # need at least 1 future day to grade
        if dates.iloc[i] < start_date:
            continue
        window = df.iloc[max(0, i - WINDOW + 1):i + 1]
        vote = evaluate_daily(window)
        if vote is None:
            continue
        votes.append({"i": i, "date": dates.iloc[i], "vote": vote})
    return votes


def walk_forward(df: pd.DataFrame, i: int, direction: str, entry: float,
                 invalidation: float, horizon_end) -> tuple[str, float] | None:
    """Same resolution rule as swing.tracker._walk_forward."""
    dates = pd.to_datetime(df["timestamp"]).dt.tz_localize(None).dt.date
    target_move = entry * cfg.SWING_TARGET_MOVE_PCT / 100.0
    is_call = direction == "CALL"
    last_close = None
    passed_horizon = False
    for j in range(i + 1, len(df)):
        d = dates.iloc[j]
        if d > horizon_end:
            passed_horizon = True
            break
        high, low, close = float(df["high"].iloc[j]), float(df["low"].iloc[j]), float(df["close"].iloc[j])
        last_close = close
        if is_call:
            if low <= invalidation:
                return "INVALIDATED", round((invalidation - entry) / entry * 100, 2)
            if high >= entry + target_move:
                return "TARGET_HIT", cfg.SWING_TARGET_MOVE_PCT
        else:
            if high >= invalidation:
                return "INVALIDATED", round((invalidation - entry) / entry * 100, 2)
            if low <= entry - target_move:
                return "TARGET_HIT", -cfg.SWING_TARGET_MOVE_PCT
    # Only grade EXPIRED when the data actually extends past the horizon —
    # signals near the end of history are unresolved, not expired (a 3-day-old
    # signal graded off a truncated walk would skew the sweep).
    if last_close is None or not passed_horizon:
        return None
    move_pct = round((last_close - entry) / entry * 100, 2)
    favorable = move_pct > 0 if is_call else move_pct < 0
    return ("EXPIRED_WIN" if favorable else "EXPIRED_LOSS"), move_pct


def simulate(ticker: str, df: pd.DataFrame, votes: list[dict],
             threshold: float, min_conf: float) -> list[dict]:
    """Apply thresholds + one-open-signal + cooldown, grade each signal."""
    signals = []
    open_until = None            # date until which the ticker is "occupied"
    last_fired: dict[str, object] = {}  # direction -> signal date (cooldown)
    for v in votes:
        vote, d = v["vote"], v["date"]
        if open_until and d <= open_until:
            continue
        if vote.direction == "NEUTRAL":
            continue
        if abs(vote.score) < threshold or vote.confidence < min_conf:
            continue
        direction = "CALL" if vote.direction == "BULLISH" else "PUT"
        last = last_fired.get(direction)
        if last and (d - last).days < cfg.SWING_COOLDOWN_DAYS:
            continue
        entry = vote.close
        stop = cfg.SWING_ATR_STOP_MULT * vote.atr14
        invalidation = entry - stop if direction == "CALL" else entry + stop
        horizon_end = d + timedelta(days=cfg.SWING_HORIZON_CAL_DAYS)
        result = walk_forward(df, v["i"], direction, entry, invalidation, horizon_end)
        last_fired[direction] = d
        if result is None:
            continue  # ran off the end of history — unresolved
        resolution, move_pct = result
        signals.append({
            "ticker": ticker, "date": d, "direction": direction,
            "entry": entry, "invalidation": round(invalidation, 2),
            "score": vote.score, "confidence": vote.confidence,
            "signals": vote.signals,
            "resolution": resolution, "move_pct": move_pct,
            "win": resolution in ("TARGET_HIT", "EXPIRED_WIN"),
        })
        open_until = horizon_end
    return signals


def report(all_signals: list[dict], verbose: bool) -> None:
    if not all_signals:
        print("No signals fired at this threshold.")
        return
    wins = sum(1 for s in all_signals if s["win"])
    n = len(all_signals)
    print(f"\nSignals: {n} | Win rate: {wins}/{n} ({wins / n * 100:.1f}%) | "
          f"Avg move: {sum(s['move_pct'] for s in all_signals) / n:+.2f}%")

    by_res = defaultdict(int)
    for s in all_signals:
        by_res[s["resolution"]] += 1
    print("Resolutions: " + " | ".join(f"{k}: {v}" for k, v in sorted(by_res.items())))

    by_ticker = defaultdict(lambda: [0, 0])
    by_dir = defaultdict(lambda: [0, 0])
    for s in all_signals:
        by_ticker[s["ticker"]][0] += s["win"]
        by_ticker[s["ticker"]][1] += 1
        by_dir[s["direction"]][0] += s["win"]
        by_dir[s["direction"]][1] += 1
    print("Per ticker:  " + " | ".join(
        f"{t}: {w}/{c} ({w / c * 100:.0f}%)" for t, (w, c) in sorted(by_ticker.items())))
    print("Per side:    " + " | ".join(
        f"{d}: {w}/{c} ({w / c * 100:.0f}%)" for d, (w, c) in sorted(by_dir.items())))

    # Per-indicator accuracy vs the actual move (with deadband), as live grading does.
    agg = defaultdict(lambda: [0, 0])
    for s in all_signals:
        if s["move_pct"] >= DEADBAND:
            actual = "BULLISH"
        elif s["move_pct"] <= -DEADBAND:
            actual = "BEARISH"
        else:
            continue
        for name, sig in s["signals"].items():
            if sig in ("BULLISH", "BEARISH"):
                agg[name][0] += (sig == actual)
                agg[name][1] += 1
    print("\nPer-indicator accuracy (graded bars):")
    for name, (c, t) in sorted(agg.items(), key=lambda x: -(x[1][0] / x[1][1] if x[1][1] else 0)):
        print(f"  {name:14s} {c:4d}/{t:<4d} {c / t * 100:5.1f}%" if t else f"  {name:14s} n/a")

    if verbose:
        print("\nAll signals:")
        for s in all_signals:
            print(f"  {s['date']} {s['ticker']:5s} {s['direction']:4s} "
                  f"entry ${s['entry']:<8.2f} score {s['score']:+5.1f} conf {s['confidence']:5.1f}% "
                  f"-> {s['resolution']:13s} {s['move_pct']:+.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the swing options strategy")
    ap.add_argument("--tickers", default=",".join(cfg.SWING_TICKERS))
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--threshold", type=float, default=cfg.SWING_SCORE_THRESHOLD)
    ap.add_argument("--min-confidence", type=float, default=cfg.SWING_MIN_CONFIDENCE)
    ap.add_argument("--sweep", action="store_true", help="sweep score thresholds 2..7")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    frames: dict[str, tuple[pd.DataFrame, list[dict]]] = {}
    for ticker in tickers:
        print(f"Loading {ticker} daily history...", flush=True)
        df = load_daily(ticker, args.start)
        if df is None or len(df) < MIN_BARS:
            print(f"  {ticker}: not enough history — skipped")
            continue
        votes = evaluate_all(df, args.start)
        frames[ticker] = (df, votes)
        print(f"  {ticker}: {len(df)} bars, {len(votes)} evaluated days")

    if not frames:
        print("Nothing to backtest.")
        return

    if args.sweep:
        print(f"\n{'threshold':>9s} {'signals':>8s} {'win rate':>9s} {'avg move':>9s}")
        for th in (2.0, 3.0, 4.0, 5.0, 6.0, 7.0):
            sigs = [s for t, (df, votes) in frames.items()
                    for s in simulate(t, df, votes, th, args.min_confidence)]
            if not sigs:
                print(f"{th:9.1f} {0:8d} {'—':>9s} {'—':>9s}")
                continue
            wins = sum(1 for s in sigs if s["win"])
            avg = sum(s["move_pct"] for s in sigs) / len(sigs)
            print(f"{th:9.1f} {len(sigs):8d} {wins / len(sigs) * 100:8.1f}% {avg:+8.2f}%")
        return

    all_signals = [s for t, (df, votes) in frames.items()
                   for s in simulate(t, df, votes, args.threshold, args.min_confidence)]
    all_signals.sort(key=lambda s: s["date"])
    print(f"\n=== Swing backtest: {', '.join(frames)} from {args.start} "
          f"(threshold {args.threshold}, min conf {args.min_confidence}%) ===")
    report(all_signals, args.verbose)


if __name__ == "__main__":
    main()
