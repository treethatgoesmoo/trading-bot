"""
Real (ATR-normalized) momentum + extension/reversal-risk score backtest.

Follow-up to backtest_extension_score.py, which tested a cheap proxy built
only from already-stored categorical vote fields and found no reversal
edge — but that version couldn't build a real velocity axis or normalize
extension by volatility (no per-row ATR stored). This version fetches raw
intraday candles (bulk yfinance, matching analyze_days.py/backtest_swing.py
— no API budget) and computes both axes properly:

  MOMENTUM SCORE (-100..100): velocity, not just direction.
    3-bar price change, in ATR units, scaled and clipped.
    >0 = accelerating up, <0 = accelerating down, near 0 = stalling.
    This is the new descriptive score's three-interval lookback. The older
    live Momentum voter uses a different two-interval move, so the two must
    not be described as the same feature.

  EXTENSION SCORE (0..100): how stretched the move is, ATR-normalized.
    0.5x |RSI-50|/50*100  +  0.5x clip(|price-VWAP| / ATR / 3.0, 0, 1)*100
    + 30 if a pivot RSI divergence (same detector as the live bot,
      zero_dte.strategy._detect_divergence, imported directly rather than
      reimplemented) fires against the momentum score's own sign. Live can
      use an auxiliary 5m divergence frame, so logged live scores remain the
      final source of truth.
    3.0 ATR is the "fully extended" cap — reported alongside the real
    distribution below so it's not an arbitrary unchecked constant.

Both axes are computed independently per stream (5m/15m — separate fetch +
separate rolling indicators). Inputs are frozen under .research-data by
default, and labels require an exact next bar in the same session.

Two questions get asked of history:
  1. Does high EXTENSION predict REVERSAL (vs the naive momentum-sign call)
     better than baseline? (top/bottom timing)
  2. Does high |MOMENTUM| predict CONTINUATION better than baseline?
     (does the velocity read actually mean anything)

This is backtest-only — no network calls beyond the initial bulk fetch, no
live code touched, nothing posts. Read docs/strategy-log.md's format before
this becomes an entry; it isn't one yet.

Usage:
    python backtest_momentum_extension.py                  # SPY, both streams, full yfinance window
    python backtest_momentum_extension.py --ticker QQQ
    python backtest_momentum_extension.py --interval 15
    python backtest_momentum_extension.py --mockup-only     # skip grading; print recent readouts
    python backtest_momentum_extension.py --refresh         # deliberately replace frozen inputs
"""

import argparse
from datetime import datetime, timedelta, time as dtime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

from core.indicators import rsi_series, atr as atr_series, macd as macd_series
from core.market_calendar import close_time
from zero_dte.strategy import _detect_divergence
from config import FORECAST_DEADBAND_ATR_MULT, RSI_DIV_LOOKBACK, RSI_PIVOT_ORDER

ET = pytz.timezone("US/Eastern")
EXTENSION_ATR_CAP = 3.0       # ATR multiples treated as "fully extended" (score 100)
MOMENTUM_ATR_CAP = 1.0        # ATR multiples of 3-bar move treated as "max velocity" (score 100)
DIVERGENCE_BONUS = 30


def fetch(ticker: str, interval: str, data_dir: str | Path = ".research-data",
          refresh: bool = False) -> pd.DataFrame:
    """Load a frozen CSV when available, otherwise fetch and cache it.

    Yahoo only exposes a short rolling intraday window. Caching the exact input
    makes a reported result reproducible instead of silently changing tomorrow.
    Use --refresh deliberately to replace the snapshot.
    """
    cache_dir = Path(data_dir)
    cache_path = cache_dir / f"{ticker.upper()}-{interval}.csv"
    metadata_path = cache_path.with_suffix(".metadata.json")
    fetched_at = None
    if cache_path.exists() and not refresh:
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        print(f"  Input: frozen {cache_path}")
    else:
        df = yf.download(
            ticker, period="60d", interval=interval, progress=False,
            auto_adjust=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df.rename(columns=str.lower)
        fetched_at = datetime.now(timezone.utc).isoformat()
    df.index = pd.to_datetime(df.index, utc=True)

    if not df.empty and (refresh or not cache_path.exists()):
        cache_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_path)
        print(f"  Input: Yahoo snapshot saved to {cache_path}")
    if not df.empty:
        digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
        if fetched_at is None and metadata_path.exists():
            saved_metadata = json.loads(metadata_path.read_text())
            expected_digest = saved_metadata.get("csv_sha256")
            if expected_digest and expected_digest != digest:
                raise RuntimeError(
                    f"Frozen snapshot checksum mismatch: {cache_path}; "
                    "restore it or use --refresh deliberately"
                )
        metadata = {
            "ticker": ticker.upper(),
            "interval": interval,
            "period": "60d",
            "provider": "yfinance/Yahoo",
            "yfinance_version": yf.__version__,
            "auto_adjust": False,
            "fetched_at_utc": fetched_at,
            "first_timestamp": df.index.min().isoformat(),
            "last_timestamp": df.index.max().isoformat(),
            "rows": len(df),
            "csv_sha256": digest,
        }
        if fetched_at is not None or not metadata_path.exists():
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    return df


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative VWAP that resets each ET session — same definition as
    zero_dte.strategy.compute_vwap, applied as a running series."""
    et_idx = df.index.tz_convert(ET)
    day = et_idx.date
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tpv = typical * df["volume"]
    grp = pd.Series(day, index=df.index)
    cum_tpv = tpv.groupby(grp).cumsum()
    cum_vol = df["volume"].groupby(grp).cumsum()
    return cum_tpv / cum_vol.replace(0, np.nan)


def build_frame(ticker: str, yf_interval: str, interval_min: int,
                data_dir: str | Path = ".research-data",
                refresh: bool = False) -> pd.DataFrame:
    raw = fetch(ticker, yf_interval, data_dir=data_dir, refresh=refresh)
    if raw.empty:
        print(f"  No data for {ticker} {yf_interval} — skipping.")
        return pd.DataFrame()

    df = raw.copy()
    df["rsi"] = rsi_series(df["close"], 14)
    df["atr"] = atr_series(df, 14)
    _, _, hist = macd_series(df["close"])
    df["macd_hist"] = hist
    df["vwap"] = session_vwap(df)
    df["mom3"] = df["close"] - df["close"].shift(3)

    n = len(df)
    momentum_score = np.full(n, np.nan)
    momentum_strength_atr = np.full(n, np.nan)
    extension_score = np.full(n, np.nan)
    naive_call = np.array([None] * n, dtype=object)
    divergence_flag = np.array([None] * n, dtype=object)

    closes = df["close"].to_numpy()
    rsis = df["rsi"].to_numpy()
    atrs = df["atr"].to_numpy()
    vwaps = df["vwap"].to_numpy()
    mom3s = df["mom3"].to_numpy()
    session_dates = np.asarray(df.index.tz_convert(ET).date)

    for i in range(RSI_DIV_LOOKBACK, n):
        a = atrs[i]
        if (np.isnan(a) or a <= 0 or np.isnan(rsis[i]) or np.isnan(mom3s[i])
                or session_dates[i - 3] != session_dates[i]):
            # A three-interval intraday score needs four closes from the same
            # session; do not blend yesterday's close into the 10:00 read.
            continue

        mom_atr = mom3s[i] / a
        m_score = float(np.clip(mom_atr / MOMENTUM_ATR_CAP * 100, -100, 100))
        momentum_score[i] = m_score
        # Keep the unclipped absolute magnitude for strength tertiles. Using
        # signed/clipped momentum here would test bullish-vs-bearish asymmetry,
        # not whether unusually strong moves continue.
        momentum_strength_atr[i] = abs(mom_atr)
        call = "BULLISH" if m_score > 0 else "BEARISH" if m_score < 0 else None
        naive_call[i] = call

        rsi_component = abs(rsis[i] - 50) / 50 * 100
        vwap_dist_atr = abs(closes[i] - vwaps[i]) / a if not np.isnan(vwaps[i]) else 0.0
        vwap_component = float(np.clip(vwap_dist_atr / EXTENSION_ATR_CAP, 0, 1) * 100)

        window_closes = pd.Series(closes[i - RSI_DIV_LOOKBACK:i + 1])
        window_rsi = pd.Series(rsis[i - RSI_DIV_LOOKBACK:i + 1])
        div = _detect_divergence(window_closes, window_rsi, lookback=RSI_DIV_LOOKBACK, order=RSI_PIVOT_ORDER)
        against = call is not None and (
            (div == "BULLISH_DIV" and call == "BEARISH")
            or (div == "BEARISH_DIV" and call == "BULLISH")
        )
        divergence_flag[i] = div
        bonus = DIVERGENCE_BONUS if against else 0

        extension_score[i] = min(100.0, 0.5 * rsi_component + 0.5 * vwap_component + bonus)

    df["momentum_score"] = momentum_score
    df["momentum_strength_atr"] = momentum_strength_atr
    df["extension_score"] = extension_score
    df["naive_call"] = naive_call
    df["divergence"] = divergence_flag
    df["interval_min"] = interval_min
    return df


def grade_forward(df: pd.DataFrame) -> pd.DataFrame:
    """Grade the exact next bar using only signal-time information.

    Rows are restricted to the live forecast window (completion at/after
    10:00 ET), an exact interval step, and the same trading session. This
    prevents a session's final bar from being labeled with the next open.
    The deadband deliberately uses signal-bar ATR, which is knowable when the
    forecast is made.
    """
    close = df["close"].to_numpy()
    atrv = df["atr"].to_numpy()
    idx_et = df.index.tz_convert(ET)
    n = len(df)
    outcome = np.array([None] * n, dtype=object)
    for i in range(n - 1):
        minutes = int(df["interval_min"].iat[i])
        current_open = idx_et[i]
        next_open = idx_et[i + 1]
        signal_time = current_open + timedelta(minutes=minutes)
        next_completion = next_open + timedelta(minutes=minutes)
        close_hour, close_minute = close_time(current_open.date())
        session_close = current_open.replace(hour=close_hour, minute=close_minute)
        if signal_time.time() < dtime(10, 0):
            continue
        if current_open.date() != next_open.date():
            continue
        if next_open - current_open != timedelta(minutes=minutes):
            continue
        if next_completion > session_close:
            continue

        a = atrv[i]
        if np.isnan(a) or a <= 0:
            continue
        move = close[i + 1] - close[i]
        deadband = FORECAST_DEADBAND_ATR_MULT * a
        call = df["naive_call"].iat[i]
        if pd.isna(call):
            continue
        if abs(move) < deadband:
            outcome[i] = "FLAT"
        else:
            actual = "BULLISH" if move > 0 else "BEARISH"
            outcome[i] = "CONTINUATION" if actual == call else "REVERSAL"
    df["outcome"] = outcome
    return df


def pct(c: int, n: int) -> float:
    return round(c / n * 100, 1) if n else 0.0


def tertile_report(df: pd.DataFrame, score_col: str, focus: str, label: str):
    sub = df[df["outcome"].notna() & df[score_col].notna()].copy()
    n = len(sub)
    if n < 30:
        print(f"  [{label}] not enough graded rows (n={n}) — skipping")
        return
    base = pct((sub["outcome"] == focus).sum(), n)
    # Keep identical scores together. Clipped/discrete scores can therefore
    # produce unequal or empty buckets; that is more honest than separating
    # ties based only on row order.
    t1, t2 = sub[score_col].quantile([1 / 3, 2 / 3])
    sub["_tertile"] = np.select(
        [sub[score_col] <= t1, sub[score_col] <= t2],
        ["Low", "Mid"], default="High"
    )
    print(f"\n  --- {label} (n={n}, baseline {focus}% = {base}) ---")
    hdr = (f"    {'Tertile':<10}{'n':>6}{'score range':>20}"
           f"{'CONT %':>10}{'REV %':>10}{'FLAT %':>10}{'CONT|move %':>13}")
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for name in ("Low", "Mid", "High"):
        b = sub[sub["_tertile"] == name]
        m = len(b)
        if not m:
            print(f"    {name:<10}{0:>6}{'(collapsed ties)':>20}")
            continue
        directional = b[b["outcome"] != "FLAT"]
        cont = pct((b["outcome"] == "CONTINUATION").sum(), m)
        rev = pct((b["outcome"] == "REVERSAL").sum(), m)
        flat = pct((b["outcome"] == "FLAT").sum(), m)
        cond = pct((directional["outcome"] == "CONTINUATION").sum(), len(directional))
        bounds = f"{b[score_col].min():.2f}..{b[score_col].max():.2f}"
        print(f"    {name:<10}{m:>6}{bounds:>20}{cont:>10}{rev:>10}{flat:>10}{cond:>13}")


def print_mockup(df: pd.DataFrame, ticker: str, interval_label: str, rows: int = 12):
    sub = df[df["momentum_score"].notna() & df["extension_score"].notna()].tail(rows)
    if sub.empty:
        print(f"  (no scoreable rows for {ticker} {interval_label})")
        return
    print(f"\n  {'Time (ET)':<19}{'Price':>9}{'Momentum':>11}{'Extension':>11}  Read")
    print("  " + "-" * 70)
    for ts, row in sub.iterrows():
        t = ts.tz_convert(ET).strftime("%Y-%m-%d %H:%M")
        m = row["momentum_score"]
        e = row["extension_score"]
        arrow = "up-strong" if m > 60 else "up-weak" if m > 15 else "down-strong" if m < -60 else "down-weak" if m < -15 else "flat"
        ext_tag = "STRETCHED" if e >= 65 else "mid" if e >= 35 else "fresh"
        div = row["divergence"]
        div_tag = f" [{div}]" if pd.notna(div) and div else ""
        print(f"  {t:<19}{row['close']:>9.2f}{m:>+11.0f}{e:>11.0f}  {arrow} / {ext_tag}{div_tag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--interval", type=str, default=None, help="5 or 15; default both")
    ap.add_argument("--mockup-only", action="store_true")
    ap.add_argument("--data-dir", default=".research-data",
                    help="frozen input directory (default: .research-data)")
    ap.add_argument("--refresh", action="store_true",
                    help="replace the frozen Yahoo snapshot deliberately")
    args = ap.parse_args()

    stream_defs = [("5m", 5), ("15m", 15)]
    if args.interval:
        want = args.interval + "m" if not args.interval.endswith("m") else args.interval
        stream_defs = [s for s in stream_defs if s[0] == want]

    print(f"\n{'='*78}")
    print(f"  Momentum + Extension score — real ATR-normalized backtest — {args.ticker}")
    print(f"  Extension ATR cap: {EXTENSION_ATR_CAP}x  |  Momentum ATR cap: {MOMENTUM_ATR_CAP}x (3-bar move)")
    print(f"{'='*78}")

    for label, minutes in stream_defs:
        print(f"\n### Stream: {label} ###")
        df = build_frame(args.ticker, label, minutes,
                         data_dir=args.data_dir, refresh=args.refresh)
        if df.empty:
            continue

        print_mockup(df, args.ticker, label)

        if args.mockup_only:
            continue

        df = grade_forward(df)
        n_graded = df["outcome"].notna().sum()
        print(f"\n  Graded rows: {n_graded}")
        # distribution check for the ATR-cap constants (sanity, not a p-value)
        vd = (df["extension_score"].notna()).sum()
        if vd:
            print(f"  extension_score distribution — median {df['extension_score'].median():.0f}, "
                  f"p90 {df['extension_score'].quantile(0.9):.0f}, max {df['extension_score'].max():.0f}")

        tertile_report(df, "extension_score", "REVERSAL", "Extension -> REVERSAL (top/bottom timing)")
        tertile_report(df, "momentum_strength_atr", "CONTINUATION",
                       "Absolute momentum strength -> CONTINUATION")

        # Divergence-against-call cross-check
        sub = df[df["outcome"].notna()]
        div_against = sub[
            ((sub["divergence"] == "BULLISH_DIV") & (sub["naive_call"] == "BEARISH")) |
            ((sub["divergence"] == "BEARISH_DIV") & (sub["naive_call"] == "BULLISH"))
        ]
        no_div = sub[~sub.index.isin(div_against.index)]
        base = pct((sub["outcome"] == "REVERSAL").sum(), len(sub)) if len(sub) else 0.0
        print(f"\n  --- Divergence-against-call cross-check (baseline REVERSAL% = {base}) ---")
        for name, part in [("Divergence fired against call", div_against), ("No divergence", no_div)]:
            m = len(part)
            rate = pct((part["outcome"] == "REVERSAL").sum(), m) if m else 0.0
            print(f"    {name:<32} n={m:<6} REVERSAL {rate}%  (edge {rate - base:+.1f}pp)")

    print(f"\n{'='*78}")
    print("  Read: extension tertiles should climb Low->High if extension finds")
    print("  real reversal zones. Momentum tertiles should climb Low->High if higher")
    print("  |velocity| means the move is more likely to keep going, not less.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
