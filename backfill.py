"""Backfill the dashboard's history by replaying the strategy over real data.

    python backfill.py            # last 30 trading days, configured tickers
    python backfill.py 20 SPY QQQ # last 20 days for SPY and QQQ

Pulls real intraday history from yfinance and replays the EXACT live logic
(strategy.compute_30m_forecast and get_daily_bias) at each historical 30-min
slot, grading every forecast/bias against what actually happened next. Writes
data/{forecasts,results,bias}-YYYY-MM-DD.* so `dashboard.py` / `!dashboard`
have something real to show.

Faithfully reconstructable: 30-min forecasts (and their per-indicator signals)
and the daily bias. NOT reconstructable here: trades and filters — those depend
on live, path-dependent intraday detection, so those panels stay empty until the
bot runs live.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, time

import pandas as pd
import pytz
import yfinance as yf

import config as cfg
from zero_dte import tracker as ft
from zero_dte.strategy import (
    compute_30m_forecast,
    get_daily_bias,
    set_bias_from_first_candle,
    FirstCandleRange,
    SMA_PERIOD,
)

ET = pytz.timezone("US/Eastern")

# Forecast slots mirror the live cadence: every 30 min from 10:00 to 15:30 ET.
SLOTS = [(h, m) for h in range(10, 16) for m in (0, 30)][:-1]  # drop 16:00


def _normalize(df: pd.DataFrame, intraday: bool) -> pd.DataFrame:
    """yfinance frame -> columns timestamp/open/high/low/close/volume in ET."""
    df = df.reset_index()
    tcol = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={tcol: "timestamp", "Open": "open", "High": "high",
                            "Low": "low", "Close": "close", "Volume": "volume"})
    ts = pd.to_datetime(df["timestamp"])
    if intraday:
        ts = ts.dt.tz_localize("UTC").dt.tz_convert(ET) if ts.dt.tz is None else ts.dt.tz_convert(ET)
    df["timestamp"] = ts
    df["date"] = ts.dt.date if intraday else pd.to_datetime(df["timestamp"]).dt.date
    return df[["timestamp", "date", "open", "high", "low", "close", "volume"]]


def _forecast_entry(ticker: str, fc, slot_dt: datetime) -> dict:
    """Same shape forecast_tracker.log_forecast writes (so results match live)."""
    return {
        "ticker": ticker,
        "time": slot_dt.isoformat(),
        "direction": fc.direction,
        "confidence": fc.confidence,
        "price": fc.current_price,
        "vwap": fc.vwap,
        "rsi_30m": fc.rsi_30m,
        "ema_direction": fc.ema_direction,
        "macd_direction": fc.macd_direction,
        "divergence": fc.divergence,
        "signals": fc.signals,
    }


async def backfill_ticker(ticker: str, days: int,
                          out: dict[str, dict[str, list]]) -> tuple[int, int]:
    tk = yf.Ticker(ticker)
    thirty = _normalize(tk.history(period="60d", interval="30m"), intraday=True)
    five = _normalize(tk.history(period="1mo", interval="5m"), intraday=True)
    daily = _normalize(tk.history(period="1y", interval="1d"), intraday=False)
    if thirty.empty or daily.empty:
        print(f"  {ticker}: no data from yfinance — skipped")
        return 0, 0

    trading_days = sorted(thirty["date"].unique())[-days:]
    n_fc = n_bias = 0

    for d in trading_days:
        ds = d.strftime("%Y-%m-%d")

        # ── Forecasts for each 30-min slot ───────────────────────────────
        for h, m in SLOTS:
            slot_dt = ET.localize(datetime.combine(d, time(h, m)))
            window = thirty[thirty["timestamp"] < slot_dt].tail(50)
            if len(window) < 15:
                continue
            outcome = thirty[thirty["timestamp"] == slot_dt]
            if outcome.empty:
                continue  # need the next candle to grade against

            fc = await compute_30m_forecast(None, ticker, data=window.copy(), asof=slot_dt)
            if fc is None:
                continue
            entry = _forecast_entry(ticker, fc, slot_dt)
            out.setdefault(ds, {}).setdefault("forecasts", []).append(entry)

            price = entry["price"]
            close = round(float(outcome.iloc[0]["close"]), 2)
            actual = "BULLISH" if close > price else "BEARISH"
            result = dict(entry)
            result["actual"] = actual
            result["actual_price"] = close
            result["move"] = round(close - price, 2)
            result["correct"] = (entry["direction"] == actual) if entry["direction"] != "NEUTRAL" else None
            out[ds].setdefault("results", []).append(result)
            n_fc += 1

        # ── Daily bias (graded vs the day's actual move) ─────────────────
        first = five[(five["date"] == d) & (five["timestamp"].dt.time == time(9, 30))]
        # Strictly before the replay day: get_daily_bias reads its last row as
        # "yesterday" — including day d would hand the morning bias today's
        # close (lookahead in the trend/RSI/multi-day components).
        daily_trunc = daily[daily["date"] < d].copy()
        day_row = daily[daily["date"] == d]
        if first.empty or day_row.empty or len(daily_trunc) < SMA_PERIOD:
            continue
        fr = first.iloc[0]
        fc_first = FirstCandleRange(ticker=ticker, high=float(fr["high"]), low=float(fr["low"]),
                                    open_price=float(fr["open"]), close_price=float(fr["close"]))
        today_open = float(fr["open"])
        bias = await get_daily_bias(None, ticker, today_open=today_open, daily=daily_trunc)
        if bias is None:
            continue
        set_bias_from_first_candle(bias, fc_first)

        drow = day_row.iloc[0]
        actual_dir = "BULLISH" if float(drow["close"]) > float(drow["open"]) else "BEARISH"
        bias_dt = ET.localize(datetime.combine(d, time(9, 35)))
        out.setdefault(ds, {}).setdefault("bias", []).append({
            "ticker": ticker, "time": bias_dt.isoformat(), "direction": bias.direction,
            "trend": bias.trend, "gap_direction": bias.gap_direction, "gap_pct": bias.gap_pct,
            "multi_day": bias.multi_day, "current_price": bias.current_price,
            "sma_value": bias.sma_value, "rsi": bias.rsi,
            "actual_direction": actual_dir,
            # NEUTRAL made no call — ungraded, matching tracker.evaluate_bias
            "correct": None if bias.direction == "NEUTRAL" else bias.direction == actual_dir,
        })
        n_bias += 1

    return n_fc, n_bias


def _write(out: dict[str, dict[str, list]], force: bool = False) -> None:
    """Write accumulated per-date data.

    Refuses to touch dates that already have live-captured files — replaying
    yfinance reconstructions over real history would silently corrupt the
    tuning inputs (and the nightly auto-commit would make it permanent).
    Pass --force to overwrite anyway.
    """
    os.makedirs(ft.DATA_DIR, exist_ok=True)
    skipped = []
    for ds, kinds in out.items():
        paths = [os.path.join(ft.DATA_DIR, f"{name}-{ds}.jsonl") for name in ("forecasts", "bias") if name in kinds]
        if "results" in kinds:
            paths.append(os.path.join(ft.DATA_DIR, f"results-{ds}.json"))
        if not force and any(os.path.exists(p) for p in paths):
            skipped.append(ds)
            continue
        for name in ("forecasts", "bias"):
            if name in kinds:
                with open(os.path.join(ft.DATA_DIR, f"{name}-{ds}.jsonl"), "w") as f:
                    for e in kinds[name]:
                        f.write(json.dumps(e) + "\n")
        if "results" in kinds:
            with open(os.path.join(ft.DATA_DIR, f"results-{ds}.json"), "w") as f:
                json.dump(kinds["results"], f, indent=2)
    if skipped:
        print(f"Skipped {len(skipped)} day(s) that already have live data "
              f"(--force overwrites): {', '.join(sorted(skipped))}")


async def main() -> None:
    args = sys.argv[1:]
    days = 30
    force = "--force" in args
    tickers = []
    for a in args:
        if a.startswith("-"):
            continue
        if a.isdigit():
            days = int(a)
        else:
            tickers.append(a.upper())
    tickers = tickers or list(cfg.TICKERS)

    print(f"Backfilling {days} trading days for {', '.join(tickers)} ...")
    out: dict[str, dict[str, list]] = {}
    totals = (0, 0)
    for t in tickers:
        fc, bias = await backfill_ticker(t, days, out)
        print(f"  {t}: {fc} forecasts, {bias} bias days")
        totals = (totals[0] + fc, totals[1] + bias)

    _write(out, force=force)
    print(f"Wrote {len(out)} day-files to {ft.DATA_DIR}")
    print(f"Total: {totals[0]} forecasts, {totals[1]} bias days")
    print("Now run:  python dashboard.py --open   (or !dashboard in Discord)")


if __name__ == "__main__":
    asyncio.run(main())
