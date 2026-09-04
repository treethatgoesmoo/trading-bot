"""Test whether completed 30m/1h candle direction predicts the next 15m bar.

This evaluates the higher-timeframe labels shown as descriptive context in the
forecast embed. They do not enter the live weighted vote. Inputs use the same
frozen Yahoo snapshots and signal-time ATR deadband as the momentum research.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest_momentum_extension import ET, fetch
from core.indicators import atr
from zero_dte.strategy import _completed_timeframe_direction


def evaluate(ticker: str, data_dir: str | Path = ".research-data",
             refresh: bool = False) -> list[dict]:
    df = fetch(ticker, "15m", data_dir=data_dir, refresh=refresh).copy()
    if df.empty:
        return []
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
    df["timestamp"] = df.index
    df["atr"] = atr(df, 14)
    totals = {30: {"correct": 0, "n": 0, "actual_bull": 0},
              60: {"correct": 0, "n": 0, "actual_bull": 0}}

    for i in range(14, len(df) - 1):
        current, nxt = df.index[i], df.index[i + 1]
        if current.date() != nxt.date() or nxt - current != pd.Timedelta(minutes=15):
            continue
        minute = current.hour * 60 + current.minute
        if not 10 * 60 <= minute < 15 * 60 + 45:
            continue
        signal_atr = float(df["atr"].iloc[i])
        if pd.isna(signal_atr) or signal_atr <= 0:
            continue
        move = float(df["close"].iloc[i + 1] - df["close"].iloc[i])
        if abs(move) <= 0.25 * signal_atr:
            continue
        actual = "BULLISH" if move > 0 else "BEARISH"
        history = df.iloc[:i + 1]
        for timeframe in (30, 60):
            signal = _completed_timeframe_direction(history, 15, timeframe)
            if signal not in ("BULLISH", "BEARISH"):
                continue
            row = totals[timeframe]
            row["n"] += 1
            row["correct"] += signal == actual
            row["actual_bull"] += actual == "BULLISH"

    return [
        {
            "ticker": ticker.upper(),
            "timeframe": timeframe,
            **values,
            "accuracy": round(values["correct"] / values["n"] * 100, 1) if values["n"] else 0.0,
            "always_bullish": round(values["actual_bull"] / values["n"] * 100, 1) if values["n"] else 0.0,
        }
        for timeframe, values in totals.items()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", default=["SPY", "QQQ", "IWM", "DIA"])
    parser.add_argument("--data-dir", default=".research-data")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    print("Completed candle direction -> exact next 15m directional outcome")
    print("Ticker  Frame       n  accuracy  always-BULL  edge")
    print("----------------------------------------------------")
    for ticker in args.tickers:
        for row in evaluate(ticker, args.data_dir, args.refresh):
            edge = row["accuracy"] - row["always_bullish"]
            print(f"{row['ticker']:<7} {row['timeframe']:>2}m  {row['n']:>6}  "
                  f"{row['accuracy']:>7.1f}%  {row['always_bullish']:>10.1f}%  {edge:>+5.1f}pp")
    print("\nContext only: these candle labels do not alter the live forecast vote.")


if __name__ == "__main__":
    main()
