---
description: Run the right backtest(s) for a strategy change and summarize the results
argument-hint: "<what changed, e.g. 'zero_dte break-and-retest stop logic' or 'swing DTE selection'>"
---
<!--
  Prompt template for backtesting a change.
  pi users: trigger as /backtest <description of what changed>  ($@ = description)
  Other toolkits: paste this file into chat; replace $@ with your description.
-->

## What changed

$@

---

## Step 1 — Pick the right backtest(s)

Map the change to the correct script(s) — run more than one if the change spans areas:

| Area of change | Script |
|---|---|
| zero_dte break-and-retest entries/stops/targets, daily bias, ATR overextension | `python backtest.py <DATE> <TICKER> --verbose` (pick a recent representative trading day + ticker; default `2026-04-13 SPY` if none obvious) |
| zero_dte 30-min / intraday forecast voting or weights | `python backtest_30m.py` |
| zero_dte filter/veto logic (entry filters) | `python backtest_filters.py` |
| candlestick pattern detectors (`core/indicators.py::detect_candlestick_patterns`) | `python backtest_patterns.py --tickers SPY,QQQ --years 10` |
| swing signal thresholds, weighted-vote logic, DTE/contract selection | `python backtest_swing.py --start 2023-01-01 --sweep` |
| Anything touching `core/indicators.py`, `core/market_data.py`, or `core/market_calendar.py` | run **both** the zero_dte and swing backtests above — shared infra affects both features |

If unsure which script(s) apply, ask before running (don't guess on a change with unclear scope).

## Step 2 — Run it

Execute the chosen command(s). These read bulk yfinance history — no `.env` or API budget required.

## Step 3 — Summarize

Report:

- Pass/fail or accuracy metrics before vs. after (if this is a re-run after a fix, compare to the prior run if available)
- Any new warnings/errors in the backtest output itself
- Whether the result supports or contradicts the intended change
- If results are ambiguous or regress, stop and flag it — don't proceed to a commit/PR on a failing backtest

## Step 4 — Log the result

If this backtest **concludes a strategy experiment** (a signal/filter/threshold is adopted, adjusted, or rejected based on these numbers), add an entry to the top of `docs/strategy-log.md`: status, what was tried, the key numbers, and the commit. Update the scoreboard table too.

## Step 5 — Smoke test

After a strategy-logic change, also run the structural smoke test to catch import breakage:

```bash
python -c "import main, bot.core, zero_dte.cog, swing.cog, core.analytics"
```
