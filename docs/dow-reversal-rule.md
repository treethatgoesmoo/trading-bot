# Dow Theory Reversal Rule — Definition & Applicability Check

**Status:** rule implemented (`core.indicators.dow_trend`) + applicability harness
(`backtest_dow.py`); **not wired into any live voter yet**. Wiring waits on backtest
numbers — this session's sandbox had no market-data egress, so the empirical run must
happen where the bot normally lives (`python backtest_dow.py`, bulk yfinance, no API
budget). The offline logic check (`python backtest_dow.py --selftest`) passes.

---

## 1. The rule

Dow Theory's trend-change ("failure swing") rule, the strict form:

- A trend is **presumed to continue** until it gives a definite reversal signal.
  Uptrend = successive **higher swing highs AND higher swing lows**.
- **Bearish reversal** fires when, after the market prints a **lower swing high**
  (a failed attempt to extend the trend), price **closes below the last confirmed
  swing low**. Both conditions — the failure *and* the support break — are required.
- **Bullish reversal** is the mirror: a higher swing low, then a close above the
  last confirmed swing high.

The looser variant (support break alone, no prior failed swing) fires earlier but
whipsaws more; `dow_trend` implements the strict form. Swings come from
`core.indicators.pivots` with `order=2` (confirmed 2 bars after the extreme —
same pivot machinery the Structure voter and RSI divergence already use), highs
from the high series and lows from the low series.

Two usable outputs, matching the two ways existing voters work:

| Output | Shape | Analogous existing voter |
|---|---|---|
| **Event** — the reversal bar itself | rare, precise, decays fast | `Cross` (20/50 SMA cross "within last 10 bars") |
| **State** — the standing Dow trend | persistent regime | `Trend` (SMA stack) |

## 2. Is it applicable to this project?

**Yes — architecturally it drops straight in.** Everything the rule needs already
exists, and nothing about it fights the design:

- **Pure math on OHLC frames** — lives in `core/indicators.py` alongside RSI/pivots,
  no network, both features can share it.
- **Voter-shaped** — it emits BULLISH/BEARISH/NEUTRAL exactly like every other
  indicator in `swing.strategy.evaluate_daily` and `zero_dte`'s
  `compute_30m_forecast`. A new `"DowReversal"` key in the signals dict is
  automatically graded (outcomes persist the full per-indicator vote dict) and
  automatically tuned (`tune_swing_weights.py` discovers indicator names from
  outcomes; new names inherit accuracy-mapped weights).
- **Backtestable with the exact live logic** — `dow_trend` is walk-forward with no
  lookahead (a pivot only counts `order` bars after it prints), so
  `backtest_dow.py` replays precisely what a live voter would have seen.

**But it overlaps an existing voter, so it must earn its seat.** The swing
`Structure` voter already votes HH/HL vs LL/LH from the same pivots (~40 closes).
The Dow rule differs in three real ways — it is *event-based* (fires on the break
bar, not a standing description), it demands *confirmation* (failure swing + level
break, where Structure needs only the pattern), and it uses *high/low series*
rather than closes — but if the backtest shows `Structure agrees` at nearly every
event bar, the rule adds weight without information and should stay out.

## 3. The check to run

```bash
python backtest_dow.py --selftest                    # offline: detector logic (passes)
python backtest_dow.py                               # daily: TSLA/AMD/GOOG/META + SPY/QQQ from 2021
python backtest_dow.py --tickers SPY --interval 5m   # 0DTE frame (yfinance keeps ~60 days of 5m)
python backtest_dow.py --verbose                     # list each reversal event
```

It grades, with the same deadband convention live tuning uses:

1. **Event accuracy** at +5/+10 daily bars (the swing horizon) — did price keep
   moving in the called direction after the reversal fired?
2. **State accuracy** on every bar — what a persistent `DowTrend` voter would score.
3. **Structure overlap** at event bars — the redundancy test above.

**Adopt as a swing voter only if** event accuracy beats ~50% *and* the Structure
voter's accuracy on the same period (compare `backtest_swing.py`'s per-indicator
table), on >50 graded events, *and* Structure isn't already agreeing at almost
every event. Expect low frequency: the strict rule historically fires only a
handful of times per ticker per year on dailies — that's fine for a voter (Cross
is also mostly NEUTRAL) but rules it out as a standalone signal stream.

## 4. Integration sketch (when the numbers justify it)

**Swing (primary fit).** In `evaluate_daily`: run `dow_trend` over the window and
vote the event direction for `DOW_EVENT_BARS` (~5) bars after a reversal, NEUTRAL
otherwise — the Cross voter's recency pattern. Add `"DowReversal": 1.5` to
`SWING_INDICATOR_WEIGHTS` (Structure's tier; let the nightly retune move it).
Zero new API calls — it reads the same 300-bar daily frame.

**0DTE (secondary, only if the 5m run looks good).** Same recency-gated vote in
`compute_30m_forecast` over the existing 50-candle frame. Intraday pivots on
5m bars are noisy and the frame holds few swings — the 5m backtest mode exists
precisely to kill this cheaply if it doesn't hold up.

**Not recommended:** using the reversal as a hard filter/veto on break-and-retest
entries. The filter stack is already audited by `backtest_filters.py`; an unproven
veto there costs expectancy invisibly. Voter-with-tuned-weight is the safe on-ramp —
the self-tuning loop will bury it if it's noise.
