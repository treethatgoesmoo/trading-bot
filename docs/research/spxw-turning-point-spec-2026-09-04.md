# SPXW turning-point and option-flow research specification

**Status:** Proposed research/shadow feature only. No live voter, trade alert, or
contract recommendation is authorized by this document.

## User constraints

- Instrument: same-day SPXW calls and puts only.
- Maximum displayed ask: $10.00 ($1,000 debit at the 100x multiplier).
- Intended hold: 15–30 minutes.
- Desired output: confirmed top/rejection, confirmed bottom/bounce, trend
  continuation, or WAIT; a contract is shown only for a confirmed setup.
- The bot remains decision support and never places an order.

## Claims this research will not make

A market top or bottom cannot be known in real time. OPRA trades paired with
NBBO quotes allow trade-aggressor inference, not knowledge of opening/closing
intent, multi-leg intent, dealer inventory, or the full resting order book.
"Absorption" below means a measurable **failed-follow-through proxy**, not
observed book absorption. The Reddit framework motivating this study was a
discretionary description, not a complete strategy; every rule must therefore
be fixed before an untouched holdout is evaluated.

The Reddit use of *delta* means buyer/seller order-flow imbalance. It is not the
same as an option contract's Greek delta.

## Data roles

- **ThetaData Indices Standard / SPX:** one-minute price, D/W/M levels, round
  levels, level acceptance/rejection. SPX index volume is not used.
- **SPY OHLCV:** regular-session VWAP and volume-weighted deviation, one-minute
  SMA20/SMA200, ATR, and same-minute-of-day relative volume. The one-minute MA
  interpretation is an explicit initial assumption because the source comment
  did not specify a timeframe.
- **ThetaData Options Standard / SPXW:** every trade paired with latest NBBO,
  narrow-chain NBBO, first-order Greeks, volume, and open interest.

Raw licensed ThetaData data lives only in ignored `.thetadata-data/`; it must
not enter tracked `data/`, logs, Discord, or Git.

## Predetermined levels

At each session start, calculate without future data:

1. Previous session O/H/L/C.
2. Current week and month opens.
3. Previous completed week and month O/H/L/C.
4. SPX levels divisible by 25 (therefore including 50 and 100).

Current-session high/low may be captured separately as developing levels but
must not be mislabeled as predetermined.

## OPRA aggressor inference

For a valid preceding NBBO no more than two seconds old:

- Trade above midpoint: inferred ask-side/buyer initiated.
- Trade below midpoint: inferred bid-side/seller initiated.
- Exact midpoint: ambiguous and excluded.
- Missing, locked, crossed, future, or stale quotes: invalid and excluded.

Directional premium signs are:

- ask-side call: bullish;
- bid-side call: bearish;
- ask-side put: bearish;
- bid-side put: bullish.

Premium is `trade price * contracts * 100`. Report call/put components,
classifiable percentage, and excluded counts alongside net flow. Before a live
rule is allowed, primary OPRA trade-condition codes and likely complex-order
prints must be profiled rather than silently treated as simple directional
orders.

## Candidate state machine (to be implemented after collection)

Continuous features are stored before thresholds are tuned: distance to each
level, VWAP deviation, SMA20/SMA200 distance and slope, ATR, relative volume,
flow components, flow acceleration, and SPX movement per dollar of inferred
flow.

The preregistered primary extension threshold is absolute 3.0 VWAP weighted
standard deviations. Any 2.0/2.5 alternatives are training-only sensitivity
checks and cannot be selected using holdout results. Relative volume must use
the same minute of prior sessions, not the preceding bars.

A **potential top** requires contact/sweep of predetermined resistance plus
upward extension and strong bullish volume/flow. It becomes a **confirmed
rejection** only after price returns below the level and bullish activity fails
to produce follow-through or flow reverses. A bottom is symmetric. Multiple
completed closes beyond the level with continued progress become **trend
continuation**, never an automatic fade. Everything else is **WAIT**.

Exact proximity, strong-flow, failed-progress, and bar-count thresholds will be
fixed using only a chronological training partition, then frozen for validation
and a final untouched holdout.

## Contract policy for shadow decisions

A confirmed bottom maps to a call and a confirmed top maps to a put. Candidate
contracts must meet all of:

- expiration equals the ET session date;
- `0 < ask <= $10.00`, bid greater than zero;
- at least 30 minutes remain before the calendar-aware close;
- quote age at most 10 seconds;
- displayed bid and ask sizes are nonzero;
- percentage spread at most 12%;
- absolute first-order delta from 0.30 through 0.65.

Rank survivors by absolute delta nearest 0.50, then lower spread, strike nearer
SPX, then displayed size. Never move farther out of the money merely to satisfy
the budget. If no contract passes, output WAIT with the failed constraint.
These thresholds are an initial shadow policy and must be evaluated, not
presented as established profitable values.

## Outcome and adoption gates

- Entry: first valid ask at or immediately after decision time.
- Exits: first valid bid at or after 15 and 30 minutes.
- Also record quote latency, maximum favorable/adverse option movement, SPX
  movement, and missing exits. Never substitute midpoint or zero.
- Compare against all level touches, price-only setup, unconditional extreme
  fade, and same-time random-direction baselines.
- Split chronologically by session. Tune on training days only; evaluate once
  on a frozen untouched holdout with day-clustered uncertainty.
- Then run prospectively in silent shadow mode. Discord recommendations remain
  disabled unless both holdout and shadow ask-to-bid returns show useful edge,
  adequate sample size, and acceptable drawdown. No result authorizes order
  execution.

## Collection

After installing dependencies and setting `THETADATA_API_KEY` on the Pi:

```bash
python collect_spxw_research.py --date YYYY-MM-DD
```

Run collection only after the session has closed. The command rejects future
or in-progress dates, validates opening-to-close SPX/SPY coverage, normalizes
all stored timestamps to UTC, and freezes SPX one-minute OHLC, SPY one-minute
OHLCV, and same-day SPXW trade/quote records around ATM. OPRA history is requested in bounded
30-minute windows and normalized with vectorized dataframe operations so a
large response does not stall the Raspberry Pi. Progress and row counts are
printed after every window. It writes request parameters, package versions,
timestamp ranges, row counts, and SHA-256
metadata, then verifies rather than replaces an existing snapshot. Publishing
uses a staging directory so a partial provider failure cannot overwrite a good
snapshot. Use `--refresh` only for an intentional replacement. `--skip-spy` is
available when Yahoo's recent minute-history window cannot serve the requested
date. Trade-triggered quote rows support flow inference; exact 15/30-minute
exits must use the candidate contract's separate quote-time history endpoint.
