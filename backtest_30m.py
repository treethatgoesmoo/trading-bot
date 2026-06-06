"""
Backtest 30-minute interval prediction accuracy.

Analyzes historical 30-min candles to test which indicators predict
the next 30-min direction, with focus on reversals.

Usage:
    python backtest_30m.py                    # last 5 trading days, SPY
    python backtest_30m.py --days 20          # last 20 trading days
    python backtest_30m.py --ticker QQQ       # different ticker
    python backtest_30m.py --days 20 --detail # show every candle prediction
"""

import sys
import argparse
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

ET = pytz.timezone("US/Eastern")


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns to a 30-min candle DataFrame."""
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    v = df["Volume"].astype(float)
    o = df["Open"].astype(float)

    # EMAs
    df["EMA9"] = c.ewm(span=9, adjust=False).mean()
    df["EMA21"] = c.ewm(span=21, adjust=False).mean()

    # RSI (14-period)
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]

    # VWAP (resets daily — computed later per day)
    df["TP"] = (h + l + c) / 3
    df["TPV"] = df["TP"] * v

    # Candle direction
    df["Direction"] = np.where(c > o, "GREEN", "RED")

    # Candle body size as % of price
    df["Body_Pct"] = ((c - o) / o * 100).round(3)

    # Upper/lower wick ratios
    body_top = np.maximum(c, o)
    body_bot = np.minimum(c, o)
    total_range = h - l
    total_range = total_range.replace(0, np.nan)
    df["Upper_Wick_Pct"] = ((h - body_top) / total_range * 100).round(1)
    df["Lower_Wick_Pct"] = ((body_bot - l) / total_range * 100).round(1)

    # Volume relative to rolling average
    df["Vol_Ratio"] = (v / v.rolling(5).mean()).round(2)

    # Momentum: close vs close 2 bars ago
    df["Momentum"] = (c - c.shift(2)).round(3)

    # ATR (5-period)
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(5).mean()

    # Divergence detection (RSI vs price over 5 bars)
    df["Divergence"] = None
    for i in range(5, len(df)):
        price_higher = float(c.iloc[i]) > float(c.iloc[i-5])
        rsi_lower = float(df["RSI"].iloc[i]) < float(df["RSI"].iloc[i-5]) if pd.notna(df["RSI"].iloc[i]) and pd.notna(df["RSI"].iloc[i-5]) else False
        price_lower = float(c.iloc[i]) < float(c.iloc[i-5])
        rsi_higher = float(df["RSI"].iloc[i]) > float(df["RSI"].iloc[i-5]) if pd.notna(df["RSI"].iloc[i]) and pd.notna(df["RSI"].iloc[i-5]) else False
        if price_higher and rsi_lower:
            df.iloc[i, df.columns.get_loc("Divergence")] = "BEARISH_DIV"
        elif price_lower and rsi_higher:
            df.iloc[i, df.columns.get_loc("Divergence")] = "BULLISH_DIV"

    return df


def generate_signals(row, prev_row) -> dict:
    """Generate directional signals from indicators for a single bar."""
    signals = {}

    # EMA crossover
    if pd.notna(row.get("EMA9")) and pd.notna(row.get("EMA21")):
        signals["EMA_Cross"] = "GREEN" if row["EMA9"] > row["EMA21"] else "RED"

    # RSI
    if pd.notna(row.get("RSI")):
        if row["RSI"] > 60:
            signals["RSI"] = "GREEN"
        elif row["RSI"] < 40:
            signals["RSI"] = "RED"
        else:
            signals["RSI"] = "NEUTRAL"

    # MACD histogram direction
    if pd.notna(row.get("MACD_Hist")):
        signals["MACD"] = "GREEN" if row["MACD_Hist"] > 0 else "RED"

    # MACD histogram momentum (increasing or decreasing)
    if prev_row is not None and pd.notna(row.get("MACD_Hist")) and pd.notna(prev_row.get("MACD_Hist")):
        if row["MACD_Hist"] > prev_row["MACD_Hist"]:
            signals["MACD_Mom"] = "GREEN"
        else:
            signals["MACD_Mom"] = "RED"

    # Volume spike
    if pd.notna(row.get("Vol_Ratio")):
        signals["Vol_Spike"] = "HIGH" if row["Vol_Ratio"] > 1.5 else "NORMAL"

    # Candle direction (current bar)
    signals["Candle"] = row.get("Direction", "NEUTRAL")

    # Momentum
    if pd.notna(row.get("Momentum")):
        signals["Momentum"] = "GREEN" if row["Momentum"] > 0 else "RED"

    # Previous candle rejection (large wick)
    if pd.notna(row.get("Upper_Wick_Pct")) and row["Upper_Wick_Pct"] > 60:
        signals["Rejection"] = "RED"
    elif pd.notna(row.get("Lower_Wick_Pct")) and row["Lower_Wick_Pct"] > 60:
        signals["Rejection"] = "GREEN"

    # Divergence (INVERTED: backtest showed 44.4%, so invert for 55.6%)
    if row.get("Divergence") == "BULLISH_DIV":
        signals["Divergence"] = "RED"
    elif row.get("Divergence") == "BEARISH_DIV":
        signals["Divergence"] = "GREEN"

    return signals


INDICATOR_WEIGHTS = {
    "RSI": 3.0,
    "EMA_Cross": 2.5,
    "VWAP": 2.0,
    "MACD": 2.0,
    "MACD_Mom": 0.5,
    "Divergence": 3.0,
    "Vol_Spike": 1.0,
    "Momentum": 1.0,
    "Candle": 0.0,
    "Rejection": 1.5,
}


def predict_next(signals: dict) -> str:
    """Weighted prediction from signals."""
    bull_score = sum(INDICATOR_WEIGHTS.get(k, 1.0) for k, v in signals.items() if v == "GREEN")
    bear_score = sum(INDICATOR_WEIGHTS.get(k, 1.0) for k, v in signals.items() if v == "RED")
    if bull_score > bear_score:
        return "GREEN"
    elif bear_score > bull_score:
        return "RED"
    return "NEUTRAL"


def run_backtest(days: int = 5, ticker: str = "SPY", detail: bool = False):
    print(f"\n{'='*70}")
    print(f"  30-MIN PREDICTION BACKTEST: {ticker} | Last {days} trading days")
    print(f"{'='*70}")

    # Fetch 30-min data
    data = yf.download(ticker, period=f"{days + 5}d", interval="30m", progress=False)
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.droplevel(1)

    if data.empty:
        print("ERROR: No data available.")
        return

    data = compute_indicators(data)

    # Track stats
    total = 0
    correct = 0
    signal_accuracy = defaultdict(lambda: {"correct": 0, "total": 0})
    reversal_stats = {"total": 0, "predicted": 0, "missed": 0}
    daily_results = defaultdict(lambda: {"correct": 0, "total": 0, "reversals": 0, "rev_predicted": 0})

    # Process by day
    dates = sorted(data.index.normalize().unique())
    for date in dates:
        day_data = data[data.index.normalize() == date].copy()
        if len(day_data) < 5:
            continue

        # Filter to regular hours
        rth = []
        for idx in day_data.index:
            ts = idx.tz_convert(ET) if idx.tzinfo else idx.tz_localize(ET)
            if (ts.hour == 9 and ts.minute >= 30) or (10 <= ts.hour < 16):
                rth.append(idx)
        day_data = day_data.loc[rth]
        if len(day_data) < 3:
            continue

        # Compute intraday VWAP
        cum_tpv = day_data["TPV"].cumsum()
        cum_vol = day_data["Volume"].astype(float).cumsum()
        day_data["VWAP"] = cum_tpv / cum_vol.replace(0, np.nan)

        date_str = date.strftime("%Y-%m-%d")
        weekday = date.strftime("%a")

        if detail:
            print(f"\n--- {date_str} ({weekday}) ---")
            print(f"  {'Time':>8} | {'Dir':>5} | {'Predict':>7} | {'Actual':>6} | {'Result':>6} | Signals")

        for i in range(1, len(day_data) - 1):
            row = day_data.iloc[i]
            prev_row = day_data.iloc[i - 1]
            next_row = day_data.iloc[i + 1]

            ts = day_data.index[i]
            ts_et = ts.tz_convert(ET) if ts.tzinfo else ts.tz_localize(ET)
            time_str = ts_et.strftime("%I:%M %p")

            signals = generate_signals(row, prev_row)

            # Add VWAP signal
            if pd.notna(row.get("VWAP")):
                close = float(row["Close"])
                signals["VWAP"] = "GREEN" if close > row["VWAP"] else "RED"

            prediction = predict_next(signals)
            actual = next_row["Direction"]
            is_correct = prediction == actual

            # Is this a reversal?
            current_dir = row["Direction"]
            is_reversal = actual != current_dir

            total += 1
            daily_results[date_str]["total"] += 1
            if is_correct:
                correct += 1
                daily_results[date_str]["correct"] += 1

            if is_reversal:
                reversal_stats["total"] += 1
                daily_results[date_str]["reversals"] += 1
                if prediction == actual:
                    reversal_stats["predicted"] += 1
                    daily_results[date_str]["rev_predicted"] += 1
                else:
                    reversal_stats["missed"] += 1

            # Track per-signal accuracy
            for sig_name, sig_val in signals.items():
                if sig_val in ("GREEN", "RED"):
                    signal_accuracy[sig_name]["total"] += 1
                    if sig_val == actual:
                        signal_accuracy[sig_name]["correct"] += 1

            if detail:
                sig_str = " ".join(f"{k}={v}" for k, v in signals.items() if v in ("GREEN", "RED"))
                result_icon = "✅" if is_correct else "❌"
                rev_mark = " ↩️" if is_reversal else ""
                print(f"  {time_str:>8} | {current_dir:>5} | {prediction:>7} | {actual:>6} | {result_icon:>4}   | {sig_str}{rev_mark}")

    # ── Results ──────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")

    if total == 0:
        print("  No predictions made.")
        return

    print(f"\n  Overall Accuracy: {correct}/{total} ({correct/total*100:.1f}%)")
    print(f"  Reversals: {reversal_stats['predicted']}/{reversal_stats['total']} predicted ({reversal_stats['predicted']/reversal_stats['total']*100:.1f}%)" if reversal_stats["total"] > 0 else "  No reversals")

    # Per-signal accuracy
    print(f"\n  --- Signal Accuracy (which indicators predict best) ---")
    sorted_signals = sorted(signal_accuracy.items(), key=lambda x: x[1]["correct"] / max(x[1]["total"], 1), reverse=True)
    for sig_name, stats in sorted_signals:
        acc = stats["correct"] / stats["total"] * 100 if stats["total"] > 0 else 0
        bar = "█" * int(acc / 5) + "░" * (20 - int(acc / 5))
        print(f"  {sig_name:>12}: {stats['correct']:>4}/{stats['total']:<4} ({acc:5.1f}%) {bar}")

    # Daily breakdown
    print(f"\n  --- Daily Breakdown ---")
    for date_str in sorted(daily_results.keys()):
        dr = daily_results[date_str]
        acc = dr["correct"] / dr["total"] * 100 if dr["total"] > 0 else 0
        rev_str = f" | Rev: {dr['rev_predicted']}/{dr['reversals']}" if dr["reversals"] > 0 else ""
        print(f"  {date_str}: {dr['correct']}/{dr['total']} ({acc:.0f}%){rev_str}")

    # Reversal analysis
    print(f"\n  --- Reversal Detection ---")
    if reversal_stats["total"] > 0:
        print(f"  Total reversals:    {reversal_stats['total']}")
        print(f"  Correctly predicted: {reversal_stats['predicted']} ({reversal_stats['predicted']/reversal_stats['total']*100:.1f}%)")
        print(f"  Missed:             {reversal_stats['missed']} ({reversal_stats['missed']/reversal_stats['total']*100:.1f}%)")
    else:
        print("  No reversals found in this period.")

    # Recommendations
    print(f"\n  --- Recommendations ---")
    if sorted_signals:
        best = sorted_signals[0]
        worst = sorted_signals[-1]
        best_acc = best[1]["correct"] / best[1]["total"] * 100 if best[1]["total"] > 0 else 0
        worst_acc = worst[1]["correct"] / worst[1]["total"] * 100 if worst[1]["total"] > 0 else 0
        print(f"  Best indicator:  {best[0]} ({best_acc:.1f}%)")
        print(f"  Worst indicator: {worst[0]} ({worst_acc:.1f}%)")

        strong = [s for s, st in sorted_signals if st["total"] > 0 and st["correct"]/st["total"] > 0.55]
        if strong:
            print(f"  Reliable (>55%): {', '.join(strong)}")
        weak = [s for s, st in sorted_signals if st["total"] > 0 and st["correct"]/st["total"] < 0.45]
        if weak:
            print(f"  Unreliable (<45%): {', '.join(weak)} — consider removing or inverting")

    if reversal_stats["total"] > 0:
        rev_rate = reversal_stats["predicted"] / reversal_stats["total"] * 100
        if rev_rate < 40:
            print(f"  Reversal detection is weak ({rev_rate:.0f}%). Consider adding:")
            print(f"    - Divergence detection (price makes new high but RSI/MACD doesn't)")
            print(f"    - Candle pattern recognition (engulfing, doji at extremes)")
            print(f"    - Volume climax detection (spike at end of trend)")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest 30-min prediction accuracy")
    parser.add_argument("--days", type=int, default=5, help="Number of trading days (default: 5)")
    parser.add_argument("--ticker", type=str, default="SPY", help="Ticker symbol (default: SPY)")
    parser.add_argument("--detail", action="store_true", help="Show every candle prediction")
    args = parser.parse_args()

    run_backtest(days=args.days, ticker=args.ticker, detail=args.detail)
