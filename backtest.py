"""
Backtest the First Candle Strategy on a specific date.

Usage:
    python backtest.py 2025-03-03
    python backtest.py 2025-03-03 TSLA
    python backtest.py 2025-03-03 SPY --verbose

Note: Backtest always uses yfinance directly since Twelve Data / Finnhub
have limited historical intraday data.
"""

import sys
import os

# Fix Windows terminal encoding for unicode output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import pytz

from config import SMA_PERIOD, RISK_REWARD_RATIO

ET = pytz.timezone("US/Eastern")


def backtest_date(date_str: str, ticker: str = "SPY", verbose: bool = False):
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    print(f"\n{'='*60}")
    print(f"  BACKTEST: {ticker} on {target_date}")
    print(f"{'='*60}")

    # ── Step 1: Daily Bias ──────────────────────────────────
    start = target_date - timedelta(days=SMA_PERIOD * 2 + 30)
    end = target_date + timedelta(days=1)

    daily = yf.download(ticker, start=str(start), end=str(end), interval="1d", progress=False)
    if isinstance(daily.columns, pd.MultiIndex):
        daily.columns = daily.columns.droplevel(1)

    if daily.empty or len(daily) < SMA_PERIOD:
        print("ERROR: Not enough daily data.")
        return

    daily["SMA"] = daily["Close"].rolling(window=SMA_PERIOD).mean()

    target_rows = daily[daily.index.date == target_date]
    if target_rows.empty:
        print(f"ERROR: No trading data for {target_date} (market closed?)")
        return

    daily_row = target_rows.iloc[-1]
    close_val = float(daily_row["Close"])
    sma_val = float(daily_row["SMA"])
    distance_pct = ((close_val - sma_val) / sma_val) * 100
    bias = "BULLISH" if close_val > sma_val else "BEARISH"

    print(f"\n--- Step 1: Daily Bias ---")
    print(f"  Close:    ${close_val:.2f}")
    print(f"  50 SMA:   ${sma_val:.2f}")
    print(f"  Distance: {distance_pct:+.2f}%")
    print(f"  Bias:     {'🟢' if bias == 'BULLISH' else '🔴'} {bias}")

    # ── Step 2: First 5-Min Candle ──────────────────────────
    intra_start = str(target_date)
    intra_end = str(target_date + timedelta(days=1))

    data_5m = yf.download(ticker, start=intra_start, end=intra_end, interval="5m", progress=False)
    if isinstance(data_5m.columns, pd.MultiIndex):
        data_5m.columns = data_5m.columns.droplevel(1)

    if data_5m.empty:
        print("\nERROR: No 5-minute data available for this date.")
        print("Note: yfinance only keeps ~60 days of intraday data.")
        return

    first_candle = None
    for idx in data_5m.index:
        ts = idx.tz_localize("UTC").astimezone(ET) if idx.tzinfo is None else idx.astimezone(ET)
        if ts.date() == target_date and ts.hour == 9 and ts.minute == 30:
            first_candle = data_5m.loc[idx]
            break

    if first_candle is None:
        print("\nERROR: Could not find the 9:30 AM candle.")
        return

    box_high = float(first_candle["High"])
    box_low = float(first_candle["Low"])
    box_open = float(first_candle["Open"])
    box_close = float(first_candle["Close"])
    candle_type = "Bullish" if box_close > box_open else "Bearish"

    print(f"\n--- Step 2: First 5-Min Candle (9:30-9:35) ---")
    print(f"  Open:       ${box_open:.2f}")
    print(f"  High (R):   ${box_high:.2f}")
    print(f"  Low  (S):   ${box_low:.2f}")
    print(f"  Close:      ${box_close:.2f}")
    print(f"  Range:      ${box_high - box_low:.2f}")
    print(f"  Type:       {candle_type}")

    # ── Step 3: Scan 1-Min Candles for Break & Retest ───────
    data_1m = yf.download(ticker, start=intra_start, end=intra_end, interval="1m", progress=False)
    if isinstance(data_1m.columns, pd.MultiIndex):
        data_1m.columns = data_1m.columns.droplevel(1)

    if data_1m.empty:
        print("\nERROR: No 1-minute data available for this date.")
        return

    candles_1m = []
    for idx in data_1m.index:
        ts = idx.tz_localize("UTC").astimezone(ET) if idx.tzinfo is None else idx.astimezone(ET)
        if ts.date() != target_date:
            continue
        if ts.hour == 9 and ts.minute >= 35:
            candles_1m.append((ts, data_1m.loc[idx]))
        elif ts.hour == 10:
            candles_1m.append((ts, data_1m.loc[idx]))
        elif ts.hour == 11 and ts.minute == 0:
            candles_1m.append((ts, data_1m.loc[idx]))

    if not candles_1m:
        print("\nERROR: No 1-minute candles in the 9:35-11:00 window.")
        return

    print(f"\n--- Step 3: Scanning {len(candles_1m)} candles (9:35 - 11:00) ---")

    broke_above = False
    broke_below = False
    signal_found = False

    for i, (ts, row) in enumerate(candles_1m):
        c_open = float(row["Open"])
        c_high = float(row["High"])
        c_low = float(row["Low"])
        c_close = float(row["Close"])
        time_str = ts.strftime("%I:%M %p")

        if verbose:
            print(f"  {time_str}  O:{c_open:.2f} H:{c_high:.2f} L:{c_low:.2f} C:{c_close:.2f}")

        if c_close > box_high and not broke_above:
            broke_above = True
            print(f"  ⬆️  BREAK ABOVE at {time_str} — Close ${c_close:.2f} > Box High ${box_high:.2f}")

        if c_close < box_low and not broke_below:
            broke_below = True
            print(f"  ⬇️  BREAK BELOW at {time_str} — Close ${c_close:.2f} < Box Low ${box_low:.2f}")

        if i < 2:
            continue

        prev_ts, prev_row = candles_1m[i - 1]
        prev_high = float(prev_row["High"])
        prev_low = float(prev_row["Low"])

        if broke_below and bias == "BEARISH" and not signal_found:
            if prev_high >= box_low * 0.999:
                if c_close < c_open and c_close < box_low:
                    stop = box_high
                    risk = stop - c_close
                    if risk > 0:
                        target = c_close - (risk * RISK_REWARD_RATIO)
                        signal_found = True
                        print(f"\n  🔴 SHORT SIGNAL at {time_str}")
                        print(f"     Entry:     ${c_close:.2f}")
                        print(f"     Stop Loss: ${stop:.2f}")
                        print(f"     Target:    ${target:.2f}")
                        print(f"     Risk:      ${risk:.2f}")
                        print(f"     Reward:    ${risk * RISK_REWARD_RATIO:.2f}")
                        _check_outcome(candles_1m[i + 1:], "SHORT", c_close, stop, target)
                        break

        if broke_above and bias == "BULLISH" and not signal_found:
            if prev_low <= box_high * 1.001:
                if c_close > c_open and c_close > box_high:
                    stop = box_low
                    risk = c_close - stop
                    if risk > 0:
                        target = c_close + (risk * RISK_REWARD_RATIO)
                        signal_found = True
                        print(f"\n  🟢 LONG SIGNAL at {time_str}")
                        print(f"     Entry:     ${c_close:.2f}")
                        print(f"     Stop Loss: ${stop:.2f}")
                        print(f"     Target:    ${target:.2f}")
                        print(f"     Risk:      ${risk:.2f}")
                        print(f"     Reward:    ${risk * RISK_REWARD_RATIO:.2f}")
                        _check_outcome(candles_1m[i + 1:], "LONG", c_close, stop, target)
                        break

    if not signal_found:
        print("\n  ⚪ No valid setup found in the 9:35-11:00 window.")

    print(f"\n{'='*60}\n")


def _check_outcome(remaining_candles, direction, entry, stop, target):
    for ts, row in remaining_candles:
        high = float(row["High"])
        low = float(row["Low"])
        time_str = ts.strftime("%I:%M %p")

        if direction == "LONG":
            if low <= stop:
                print(f"     ❌ STOPPED OUT at {time_str} (hit ${stop:.2f})")
                return
            if high >= target:
                print(f"     ✅ TARGET HIT at {time_str} (hit ${target:.2f})")
                return
        else:
            if high >= stop:
                print(f"     ❌ STOPPED OUT at {time_str} (hit ${stop:.2f})")
                return
            if low <= target:
                print(f"     ✅ TARGET HIT at {time_str} (hit ${target:.2f})")
                return

    print(f"     ⏳ Trade still open at end of window.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python backtest.py YYYY-MM-DD [TICKER] [--verbose]")
        print("Example: python backtest.py 2025-03-03")
        print("         python backtest.py 2025-03-03 TSLA --verbose")
        sys.exit(1)

    date = sys.argv[1]
    ticker = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else "SPY"
    verbose = "--verbose" in sys.argv

    backtest_date(date, ticker, verbose)
