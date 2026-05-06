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

import numpy as np
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import pytz

from config import (
    SMA_PERIOD, RISK_REWARD_RATIO,
    RVOL_THRESHOLD, RVOL_LOOKBACK,
    ATR_PERIOD, ATR_OVEREXTENDED_MULT,
    EMA_FAST, EMA_SLOW,
)

ET = pytz.timezone("US/Eastern")


def _compute_atr(df_5m: pd.DataFrame, period: int = ATR_PERIOD) -> float | None:
    if len(df_5m) < period + 1:
        return None
    highs = df_5m["High"].astype(float)
    lows = df_5m["Low"].astype(float)
    closes = df_5m["Close"].astype(float)
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    return float(atr) if not np.isnan(atr) else None


def _compute_vwap(candles_1m: list) -> float | None:
    if not candles_1m:
        return None
    cum_tpv = 0.0
    cum_vol = 0.0
    for _, row in candles_1m:
        tp = (float(row["High"]) + float(row["Low"]) + float(row["Close"])) / 3
        vol = float(row["Volume"])
        cum_tpv += tp * vol
        cum_vol += vol
    return cum_tpv / cum_vol if cum_vol > 0 else None


def _compute_rvol(candles_1m: list, idx: int, lookback: int = RVOL_LOOKBACK) -> tuple[bool, float]:
    if idx < lookback:
        return True, 0.0
    prior_vols = [float(candles_1m[j][1]["Volume"]) for j in range(max(0, idx - lookback), idx)]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
    if avg_vol == 0:
        return True, 0.0
    curr_vol = float(candles_1m[idx][1]["Volume"])
    ratio = round(curr_vol / avg_vol, 2)
    return ratio >= RVOL_THRESHOLD, ratio


def _compute_ema_crossover(df_5m: pd.DataFrame) -> tuple[str, float, float] | None:
    if len(df_5m) < EMA_SLOW:
        return None
    closes = df_5m["Close"].astype(float)
    ema_fast = closes.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = closes.ewm(span=EMA_SLOW, adjust=False).mean()
    fast_val = round(float(ema_fast.iloc[-1]), 2)
    slow_val = round(float(ema_slow.iloc[-1]), 2)
    direction = "BULLISH" if fast_val > slow_val else "BEARISH"
    return direction, fast_val, slow_val


def _apply_filters(direction, entry_price, candles_1m, signal_idx, df_5m_at_signal):
    """Apply all 4 filters at signal time. Returns (all_passed, filter_results)."""
    results = []

    # VWAP
    vwap = _compute_vwap(candles_1m[:signal_idx + 1])
    if vwap is not None:
        if direction == "LONG":
            passed = entry_price > vwap
        else:
            passed = entry_price < vwap
        results.append(("VWAP", passed, f"Entry ${entry_price:.2f} vs VWAP ${vwap:.2f}"))
    else:
        results.append(("VWAP", True, "No data"))

    # RVOL
    rvol_passed, rvol_ratio = _compute_rvol(candles_1m, signal_idx)
    results.append(("RVOL", rvol_passed, f"{rvol_ratio}x avg (threshold {RVOL_THRESHOLD}x)"))

    # EMA
    ema_result = _compute_ema_crossover(df_5m_at_signal)
    if ema_result is not None:
        ema_dir, ema_f, ema_s = ema_result
        if direction == "LONG":
            passed = ema_dir == "BULLISH"
        else:
            passed = ema_dir == "BEARISH"
        results.append(("EMA", passed, f"9 EMA ${ema_f} vs 21 EMA ${ema_s} ({ema_dir})"))
    else:
        results.append(("EMA", True, "Insufficient data"))

    all_passed = all(r[1] for r in results)
    return all_passed, results


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
    first_candle_idx = None
    for idx_pos, idx in enumerate(data_5m.index):
        ts = idx.tz_localize("UTC").astimezone(ET) if idx.tzinfo is None else idx.astimezone(ET)
        if ts.date() == target_date and ts.hour == 9 and ts.minute == 30:
            first_candle = data_5m.loc[idx]
            first_candle_idx = idx_pos
            break

    if first_candle is None:
        print("\nERROR: Could not find the 9:30 AM candle.")
        return

    box_high = float(first_candle["High"])
    box_low = float(first_candle["Low"])
    box_open = float(first_candle["Open"])
    box_close = float(first_candle["Close"])
    candle_type = "Bullish" if box_close > box_open else "Bearish"
    range_size = box_high - box_low

    print(f"\n--- Step 2: First 5-Min Candle (9:30-9:35) ---")
    print(f"  Open:       ${box_open:.2f}")
    print(f"  High (R):   ${box_high:.2f}")
    print(f"  Low  (S):   ${box_low:.2f}")
    print(f"  Close:      ${box_close:.2f}")
    print(f"  Range:      ${range_size:.2f}")
    print(f"  Type:       {candle_type}")

    # ATR check
    atr_data = data_5m.iloc[:first_candle_idx + 1] if first_candle_idx is not None else data_5m
    atr = _compute_atr(atr_data)
    if atr is not None:
        overextended = range_size > ATR_OVEREXTENDED_MULT * atr
        status = "⚠️  OVEREXTENDED" if overextended else "✅ Normal"
        print(f"  ATR (14):   ${atr:.2f}")
        print(f"  ATR Check:  {status} (range ${range_size:.2f} vs {ATR_OVEREXTENDED_MULT}x ATR ${atr * ATR_OVEREXTENDED_MULT:.2f})")
        if overextended:
            print(f"\n  ⚠️  Opening range overextended — would skip signals for this session.")
            print(f"\n{'='*60}\n")
            return
    else:
        print(f"  ATR:        Not enough data")

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
        if ts.hour == 9 and ts.minute >= 30:
            candles_1m.append((ts, data_1m.loc[idx]))
        elif ts.hour == 10:
            candles_1m.append((ts, data_1m.loc[idx]))
        elif ts.hour == 11 and ts.minute == 0:
            candles_1m.append((ts, data_1m.loc[idx]))

    if not candles_1m:
        print("\nERROR: No 1-minute candles in the 9:30-11:00 window.")
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

        # Get 5-min data up to signal time for EMA
        df_5m_at_signal = data_5m[data_5m.index <= ts]

        if broke_below and bias == "BEARISH" and not signal_found:
            if prev_high >= box_low * 0.999:
                if c_close < c_open and c_close < box_low:
                    stop = box_high
                    risk = stop - c_close
                    if risk > 0:
                        all_passed, filter_results = _apply_filters(
                            "SHORT", c_close, candles_1m, i, df_5m_at_signal)

                        target = c_close - (risk * RISK_REWARD_RATIO)
                        signal_found = True
                        print(f"\n  🔴 SHORT SIGNAL at {time_str}")
                        print(f"     Entry:     ${c_close:.2f}")
                        print(f"     Stop Loss: ${stop:.2f}")
                        print(f"     Target:    ${target:.2f}")
                        print(f"     Risk:      ${risk:.2f}")
                        print(f"     Reward:    ${risk * RISK_REWARD_RATIO:.2f}")

                        print(f"\n     --- Filters ---")
                        for name, passed, reason in filter_results:
                            icon = "✅" if passed else "❌"
                            print(f"     {icon} {name}: {reason}")
                        if not all_passed:
                            print(f"     ⚠️  WOULD BE FILTERED — signal blocked")
                        else:
                            _check_outcome(candles_1m[i + 1:], "SHORT", c_close, stop, target)
                        break

        if broke_above and bias == "BULLISH" and not signal_found:
            if prev_low <= box_high * 1.001:
                if c_close > c_open and c_close > box_high:
                    stop = box_low
                    risk = c_close - stop
                    if risk > 0:
                        all_passed, filter_results = _apply_filters(
                            "LONG", c_close, candles_1m, i, df_5m_at_signal)

                        target = c_close + (risk * RISK_REWARD_RATIO)
                        signal_found = True
                        print(f"\n  🟢 LONG SIGNAL at {time_str}")
                        print(f"     Entry:     ${c_close:.2f}")
                        print(f"     Stop Loss: ${stop:.2f}")
                        print(f"     Target:    ${target:.2f}")
                        print(f"     Risk:      ${risk:.2f}")
                        print(f"     Reward:    ${risk * RISK_REWARD_RATIO:.2f}")

                        print(f"\n     --- Filters ---")
                        for name, passed, reason in filter_results:
                            icon = "✅" if passed else "❌"
                            print(f"     {icon} {name}: {reason}")
                        if not all_passed:
                            print(f"     ⚠️  WOULD BE FILTERED — signal blocked")
                        else:
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
