"""Research the documented opening-to-closing intraday momentum effect.

This is deliberately separate from the live rolling forecast. It implements
Gao, Han, Li & Zhou (JFE 2018): the previous-close-to-10:00 ET return predicts
the final half-hour return. Related evidence finds a stronger effect on
high-volume/high-volatility days.

The script uses a frozen 5-minute snapshot by default, reports chronological
halves, and never changes live strategy code. Yahoo's intraday window is only
about 60 days, so this is an exploratory replication—not an adoption test.
For a real validation, point --data-dir at frozen snapshots from a longer
licensed dataset with the same CSV shape.

Sources:
  https://doi.org/10.1016/j.jfineco.2018.05.009
  https://doi.org/10.1016/j.jfineco.2021.04.029

Usage:
  python backtest_open_close_momentum.py
  python backtest_open_close_momentum.py --ticker QQQ --refresh
  python backtest_open_close_momentum.py --csv-output open-close-daily.csv
"""

from __future__ import annotations

import argparse
from datetime import timedelta

import numpy as np
import pandas as pd

from backtest_momentum_extension import ET, fetch
from core.market_calendar import close_time, is_trading_day


def build_daily_sample(bars: pd.DataFrame, volume_lookback: int = 20) -> pd.DataFrame:
    """Construct one non-overlapping observation per complete trading day."""
    if bars.empty:
        return pd.DataFrame()

    frame = bars.copy().sort_index()
    et_index = frame.index.tz_convert(ET)
    frame["_session"] = et_index.date
    rows: list[dict] = []
    previous_close: float | None = None
    previous_session = None

    for session, day in frame.groupby("_session", sort=True):
        close_hour, close_minute = close_time(session)
        session_open = ET.localize(pd.Timestamp(session).to_pydatetime()).replace(
            hour=9, minute=30
        )
        session_close = session_open.replace(hour=close_hour, minute=close_minute)
        idx_et = day.index.tz_convert(ET)
        regular = day[(idx_et >= session_open) & (idx_et < session_close)].copy()
        if regular.empty:
            continue

        regular_idx = regular.index.tz_convert(ET)
        expected_last_open = session_close - timedelta(minutes=5)
        complete = regular_idx[-1] == expected_last_open
        if not complete:
            # Do not use a partial session as either a sample or tomorrow's
            # previous close.
            previous_close = None
            previous_session = None
            continue

        day_close = float(regular["close"].iloc[-1])
        opening_end = session_open.replace(hour=10, minute=0)
        opening = regular[regular.index.tz_convert(ET) < opening_end]
        final_start = session_close - timedelta(minutes=30)
        final_half = regular[regular.index.tz_convert(ET) >= final_start]

        expected_previous = session - timedelta(days=1)
        while not is_trading_day(expected_previous):
            expected_previous -= timedelta(days=1)
        has_immediate_previous = previous_session == expected_previous
        if (previous_close is not None and has_immediate_previous
                and len(opening) == 6 and len(final_half) == 6):
            opening_close = float(opening["close"].iloc[-1])
            final_open = float(final_half["open"].iloc[0])
            opening_volume = float(opening["volume"].sum())
            opening_range = float(opening["high"].max() - opening["low"].min())
            rows.append({
                "date": session,
                "opening_return": opening_close / previous_close - 1.0,
                "final_half_return": day_close / final_open - 1.0,
                "opening_volume": opening_volume,
                "opening_range_pct": opening_range / previous_close,
            })
        previous_close = day_close
        previous_session = session

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = result.set_index("date")
    # Ex-ante regime measures: each day is compared only with prior sessions.
    result["relative_opening_volume"] = (
        result["opening_volume"]
        / result["opening_volume"].rolling(volume_lookback, min_periods=5).median().shift(1)
    )
    result["relative_opening_range"] = (
        result["opening_range_pct"]
        / result["opening_range_pct"].rolling(volume_lookback, min_periods=5).median().shift(1)
    )
    return result


def bootstrap_correlation(sample: pd.DataFrame, seed: int = 7,
                          draws: int = 5000) -> tuple[float, float] | None:
    clean = sample[["opening_return", "final_half_return"]].dropna()
    if len(clean) < 20:
        return None
    values = clean.to_numpy()
    rng = np.random.default_rng(seed)
    correlations = []
    for _ in range(draws):
        draw = values[rng.integers(0, len(values), len(values))]
        if np.std(draw[:, 0]) == 0 or np.std(draw[:, 1]) == 0:
            continue
        correlations.append(np.corrcoef(draw[:, 0], draw[:, 1])[0, 1])
    if not correlations:
        return None
    return tuple(np.quantile(correlations, [0.025, 0.975]))


def report(sample: pd.DataFrame, label: str) -> None:
    clean = sample[["opening_return", "final_half_return"]].dropna()
    print(f"\n  {label}: n={len(clean)}")
    if len(clean) < 5:
        print("    Insufficient observations")
        return
    x = clean["opening_return"].to_numpy()
    y = clean["final_half_return"].to_numpy()
    correlation = float(np.corrcoef(x, y)[0, 1]) if np.std(x) and np.std(y) else float("nan")
    slope = float(np.polyfit(x, y, 1)[0]) if np.std(x) else float("nan")
    agreement = float((np.sign(x) == np.sign(y)).mean() * 100)
    bull_baseline = float((y > 0).mean() * 100)
    strategy_returns = np.sign(x) * y
    ci = bootstrap_correlation(clean)
    ci_text = f" [{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else " (n<20; no CI)"
    print(f"    correlation {correlation:+.3f}{ci_text}; slope {slope:+.3f}")
    print(f"    directional agreement {agreement:.1f}% | always-BULL {bull_baseline:.1f}%")
    print(f"    sign-strategy mean {strategy_returns.mean() * 10_000:+.2f} bp/day "
          f"(gross, underlying, no costs)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--data-dir", default=".research-data")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--volume-lookback", type=int, default=20)
    parser.add_argument("--csv-output", default=None)
    args = parser.parse_args()

    bars = fetch(args.ticker, "5m", data_dir=args.data_dir, refresh=args.refresh)
    sample = build_daily_sample(bars, volume_lookback=args.volume_lookback)
    if sample.empty:
        raise SystemExit("No complete daily samples available")

    print("\n" + "=" * 78)
    print(f"  Opening -> closing-half-hour momentum — {args.ticker}")
    print("  Predictor: previous close -> 10:00 ET | Target: final 30 minutes")
    print("=" * 78)
    report(sample, "All available days (exploratory)")

    split = len(sample) // 2
    report(sample.iloc[:split], "First chronological half")
    holdout = sample.iloc[split:]
    report(holdout, "Second chronological half")
    report(holdout[holdout["relative_opening_volume"] >= 1.0],
           "Second half: above-normal opening volume")
    report(holdout[holdout["relative_opening_range"] >= 1.0],
           "Second half: above-normal opening range")

    if args.csv_output:
        sample.to_csv(args.csv_output)
        print(f"\n  Wrote derived daily sample: {args.csv_output}")

    print("\n  Decision rule: exploratory Yahoo results are not sufficient for adoption.")
    print("  Require a multi-year frozen dataset and untouched chronological holdout.\n")


if __name__ == "__main__":
    main()
