"""
Analyze recent trading days — compare first candle range, bias, and outcomes.

Usage:
    python analyze_days.py          # last 5 trading days
    python analyze_days.py 10       # last 10 trading days
    python analyze_days.py 5 QQQ    # last 5 days for QQQ
"""

import sys
import yfinance as yf
import pandas as pd
import numpy as np
import pytz

ET = pytz.timezone("US/Eastern")


def analyze(days: int = 5, ticker: str = "SPY"):
    # Fetch intraday data
    data_5m = yf.download(ticker, period=f"{days + 3}d", interval="5m", progress=False)
    if isinstance(data_5m.columns, pd.MultiIndex):
        data_5m.columns = data_5m.columns.droplevel(1)

    daily = yf.download(ticker, period="6mo", interval="1d", progress=False)
    if isinstance(daily.columns, pd.MultiIndex):
        daily.columns = daily.columns.droplevel(1)

    if data_5m.empty or daily.empty:
        print("ERROR: Could not fetch data.")
        return

    # Compute daily indicators
    daily["SMA50"] = daily["Close"].rolling(50).mean()
    delta = daily["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    daily["RSI"] = 100 - (100 / (1 + avg_gain / avg_loss))

    # Count last 5 days green/red
    results = {"correct": 0, "wrong": 0, "total": 0}

    for date in sorted(data_5m.index.normalize().unique()):
        day_data = data_5m[data_5m.index.normalize() == date]
        if day_data.empty:
            continue

        # Find first 5-min candle
        first_candle = None
        for idx in day_data.index:
            ts = idx.tz_convert(ET) if idx.tzinfo else idx.tz_localize(ET)
            if ts.hour == 9 and ts.minute == 30:
                first_candle = day_data.loc[idx]
                break

        if first_candle is None:
            continue

        box_high = float(first_candle["High"])
        box_low = float(first_candle["Low"])
        box_open = float(first_candle["Open"])
        box_close = float(first_candle["Close"])
        fc_type = "Bullish" if box_close > box_open else "Bearish"

        # Regular trading hours
        rth = []
        for idx in day_data.index:
            ts = idx.tz_convert(ET) if idx.tzinfo else idx.tz_localize(ET)
            if (ts.hour == 9 and ts.minute >= 30) or (10 <= ts.hour < 16):
                rth.append(day_data.loc[idx])

        if not rth:
            continue

        rth_df = pd.DataFrame(rth)
        day_high = float(rth_df["High"].max())
        day_low = float(rth_df["Low"].min())
        day_open = float(rth_df.iloc[0]["Open"])
        day_close = float(rth_df.iloc[-1]["Close"])
        day_dir = "GREEN" if day_close > day_open else "RED"
        day_chg = ((day_close - day_open) / day_open) * 100

        # Get daily row
        d_row = daily[daily.index.date == date.date()]
        sma = float(d_row["SMA50"].iloc[-1]) if not d_row.empty and not pd.isna(d_row["SMA50"].iloc[-1]) else None
        rsi = float(d_row["RSI"].iloc[-1]) if not d_row.empty and not pd.isna(d_row["RSI"].iloc[-1]) else None

        # Previous day
        daily_idx = daily.index.get_loc(d_row.index[0]) if not d_row.empty and d_row.index[0] in daily.index else -1
        if isinstance(daily_idx, slice):
            daily_idx = daily_idx.start
        if daily_idx > 0:
            prev_d = daily.iloc[daily_idx - 1]
            prev_dir = "GREEN" if float(prev_d["Close"]) > float(prev_d["Open"]) else "RED"
            prev_open = float(prev_d["Open"])
            prev_close = float(prev_d["Close"])
            prev_high = float(prev_d["High"])
            prev_low = float(prev_d["Low"])
        else:
            prev_dir = "N/A"
            prev_open = prev_close = prev_high = prev_low = 0

        # 5-day lookback
        if isinstance(daily_idx, int) and daily_idx >= 5:
            last5 = daily.iloc[daily_idx-5:daily_idx]
            g5 = int((last5["Close"].astype(float) > last5["Open"].astype(float)).sum())
            r5 = 5 - g5
        else:
            g5 = r5 = 0

        # Prev-day momentum baseline — NOT the live bias model (that one is
        # 50 SMA + gap + first candle + RSI, graded in data/bias-*.jsonl /
        # !stats); this is a simple yardstick to compare days against.
        bot_bias = "BULLISH" if prev_dir == "GREEN" else "BEARISH"
        correct = (bot_bias == "BULLISH" and day_dir == "GREEN") or (bot_bias == "BEARISH" and day_dir == "RED")

        broke_above = day_high > box_high
        broke_below = day_low < box_low

        results["total"] += 1
        if correct:
            results["correct"] += 1
        else:
            results["wrong"] += 1

        date_str = date.strftime("%Y-%m-%d")
        weekday = date.strftime("%a")

        print(f"\n{'='*65}")
        print(f"  {date_str} ({weekday}) — Day was {day_dir} ({day_chg:+.2f}%)")
        print(f"{'='*65}")
        print(f"  First 5m:   O:${box_open:.2f} H:${box_high:.2f} L:${box_low:.2f} C:${box_close:.2f} ({fc_type})")
        print(f"              Range: ${box_high - box_low:.2f}")
        print(f"  Day:        O:${day_open:.2f} H:${day_high:.2f} L:${day_low:.2f} C:${day_close:.2f}")
        print(f"  Prev Day:   {prev_dir} | O:${prev_open:.2f} C:${prev_close:.2f} H:${prev_high:.2f} L:${prev_low:.2f}")
        print(f"  Baseline:   {bot_bias} (prev-day momentum: {prev_dir}) — {'✅ CORRECT' if correct else '❌ WRONG'}")
        print(f"  5-Day:      {g5}G / {r5}R")
        if sma:
            print(f"  50 SMA:     ${sma:.2f} ({'above' if day_close > sma else 'below'})")
        if rsi:
            rsi_label = "Overbought" if rsi >= 70 else ("Oversold" if rsi <= 30 else "Neutral")
            print(f"  RSI:        {rsi:.1f} ({rsi_label})")
        print(f"  Box Breaks: Above={'YES' if broke_above else 'NO'} | Below={'YES' if broke_below else 'NO'}")

        # What happened after the box break
        if broke_above and broke_below:
            print(f"  -> Both sides broken — choppy day")
        elif broke_above:
            if day_close > box_high:
                print(f"  -> Broke above and held — long worked (+${day_close - box_high:.2f} from box)")
            else:
                print(f"  -> Broke above but faded back — false breakout")
        elif broke_below:
            if day_close < box_low:
                print(f"  -> Broke below and held — short worked (${box_low - day_close:.2f} from box)")
            else:
                print(f"  -> Broke below but bounced back — false breakdown")
        else:
            print(f"  -> Stayed inside box all day")

        # Signal alignment check
        if broke_above and bot_bias == "BULLISH":
            print(f"  Signal:     LONG setup aligned ✅")
        elif broke_below and bot_bias == "BEARISH":
            print(f"  Signal:     SHORT setup aligned ✅")
        elif broke_above and bot_bias == "BEARISH":
            print(f"  Signal:     Broke above but bias BEARISH — missed long ⚠️")
        elif broke_below and bot_bias == "BULLISH":
            print(f"  Signal:     Broke below but bias BULLISH — missed short ⚠️")

    print(f"\n{'='*65}")
    print(f"  SUMMARY: {results['correct']}/{results['total']} prev-day-momentum baseline calls correct (live bias accuracy: see !stats) ({results['correct']/results['total']*100:.0f}%)" if results["total"] > 0 else "  No data")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
    ticker = sys.argv[2] if len(sys.argv) > 2 else "SPY"
    analyze(days, ticker)
