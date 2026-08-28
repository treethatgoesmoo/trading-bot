# State of the Code — as of 2026-08-15 (see "Feature-removal pass" section below for the latest)

## Feature-removal pass — 2026-08-15

Owner request: the live channel had too many disconnected alert streams and
it was distracting. Kept the 15m forecast ensemble (the one stream with
measured per-indicator edge) and the Bollinger band watcher; removed
everything else that wasn't earning its slot on the graded evidence below,
and added a volume-spike watcher as a replacement low-cost stream.

**Removed (code deleted or disconnected from the live bot; historical
`data/` files untouched):**

- **Morning bias** (50 SMA + gap + first-candle 2-of-3 vote) — already
  demoted to informational-only 2026-08-11 after grading 50.9% directional
  vs a 52.6% always-BULLISH baseline (no measurable edge, see below). Now
  removed from the live bot entirely: no more computation, posting, `!bias`
  command, or nightly grading. `zero_dte/strategy.py::get_daily_bias` /
  `DailyBias` / `FirstCandleRange` / `set_bias_from_first_candle` are kept
  **only** because `backfill.py --bias-report` still replays the rule against
  history on demand — nothing in the live bot calls them anymore.
- **Break-and-retest entries + trade tracking** — removed along with the
  bias it depended on (`check_for_setup`, `TradeSignal`/`FilteredSignal`
  dataclasses, the entry filter stack, Take/Skip/Modify buttons, `!range`
  `!test` commands, the Daily Summary / Monitoring-started-ended messages).
  No honest replay of this system currently exists (see the backfill.py note
  under Commands) — `backtest_filters.py` still audits the historical
  `data/filters-*.jsonl` record left behind by the live years it ran.
- **5-minute companion forecast stream** (`FORECAST_5M_ENABLED`) — never beat
  its own always-BULLISH baseline (51.4% vs 55.4%, see below) and was
  already not posted; now the computation/logging is gone too.
- **Standalone VIX spike/regime watcher + `!vix` command** — the VIX vote
  *inside* the 15m/5m forecast ensemble (`core/vix.py::forecast_signal`) is
  unaffected and still live; only the separate alert stream (regime-cross
  and spike pings, `VixWatcher`) and its nightly grading were removed.
- **Standalone candlestick pattern watcher + `!patterns` command** — daily
  and weekly patterns had already backtested to no directional edge
  (docs/strategy-log.md); the 1h horizon was still unbacktested when this
  was cut. `core.indicators.detect_candlestick_patterns` stays —
  `backtest_patterns.py` still uses it for offline research.

**Added:**

- **Volume-spike watcher** (`zero_dte/volume_watcher.py`, `VOLUME_WATCH_*`
  config) — mirrors the BB watcher's shape: pings once when a closed 5m
  bar's volume clears `VOLUME_SPIKE_MULT` (default 2.0x) times its trailing
  `VOLUME_SPIKE_LOOKBACK`-bar average, re-arms after a bar back under the
  threshold, cooldown-gated, graded nightly (`evaluate_volume_alerts`) the
  same continuation-vs-reversal way the old VIX-ping grading worked. It has
  **no track record yet** — same "prove it or lose the slot" bar as every
  other stream here; watch `data/volume_alerts-*.jsonl` and the nightly
  grading before trusting it.

See `CLAUDE.md`'s `zero_dte/` description and the Commands table for the
current (post-removal) command list and config knobs.

---

# State of the Code — as of 2026-08-11 (superseded by the pass above for
what's live; the graded numbers below are what justified each removal)

A point-in-time snapshot of what's live, what the graded data says, what was
just fixed, and what's deliberately still open. Companion to
`docs/strategy-log.md` (the experiment-by-experiment record) and `CLAUDE.md`
(architecture + conventions). Refresh this doc when the picture changes
materially; the strategy log stays the append-only history.

## What's running

| Feature | Status | Notes |
|---|---|---|
| 0DTE first-candle bias + break-and-retest (zero_dte) | live | Bias demoted to informational 2026-08-11 (graded 50.9% vs 52.6% always-BULLISH): entries fire either direction, NEUTRAL still stands down; LONG entries carry a below-breakeven warning (3/34, −21.8R lifetime) |
| Morning day forecast (direction + ATR-projected high/low) | **removed 2026-08-12** | Replaced by the actual session high/low tracked on every alert |
| Intraday weighted-vote forecasts, 15m + 5m streams | live | Change-driven posting; VOLATILE class for conflicts; per-stream tuned weights as of today |
| VIX / Bollinger-touch watchers | live | BB watcher added 2026-08-07 (1m+5m, yfinance-only). RSI watcher + `!rsi` removed 2026-08-12 (already default-off; caution line lives on forecast embeds) |
| SPX 0DTE range feed (`zero_dte/spx_options.py`) | **removed 2026-08-12** | No options coverage remains — alerts are underlying-price signals for manual SPX trades |
| SPY ATM option projection + EOD grading | **removed 2026-08-11** | Owner trades SPX only; historical `option_projections`/`option_outcomes` files remain, kinds retired |
| Swing signals (TSLA/AMD/GOOG/META, 7–14 DTE picks) | live | Pre-open scan + midday confirmation; self-heals via `swing_open.json` |
| Nightly self-tune + git auto-commit of `data/` | live | Both tuners now shrinkage-damped and step-capped (see below) |

## What the graded data says (all sample sizes still small)

- **Morning bias: the 2-of-3 vote has no measurable edge.** Honest 60-day
  replay (2026-08-11, report-only mode): 50.9% directional (29/57) vs a
  52.6% always-BULLISH bar on the same days; bullish calls sit exactly on
  drift, bearish calls 47.4%. The 65% adoption number was lookahead
  artifact; the 5/6 live record was small-sample luck. (±13p at n=57 — not
  provably worse than the baseline, but nothing measurable in its favor.)
- **15m forecast stream:** a real ensemble once measured per-stream — RSI
  61.3% (n=75), VWAP 58.9% (n=158), MACD 57.6% (n=158), Momentum 56.3%
  (n=158) against a 53.8% always-BULLISH baseline.
- **5m companion stream:** no measurable edge — always-BULLISH grades 55.4%,
  at or above every 5m indicator. Its directional value is an open question.
- **Confidence is uncalibrated:** stated 70–80% → 53.8% realized, 80–90% →
  53.1%, 90–100% → 53.4% (810 directional calls). Read posted confidence as
  vote margin, not win probability. VOLATILE leans grade 48.7% — that class
  is correctly signaling "don't trust this."
- **Indicator overlap:** EMA+RSI agree 100% (15m, n=113); RSI+VWAP
  96.5–99.5%; EMA+VWAP 86.5% (5m). RSI/EMA/VWAP are one trend signal voted
  three times — the margin (and stated confidence) double-counts it.

## Fixed in the 2026-08-11 session (branch `claude/code-review-spsv37`)

1. **Bias replay lookahead** — `backfill.py` fed the replay day's completed
   bar to `get_daily_bias`, so the 50 SMA trend + RSI override saw the close
   at 9:35 AM. Live had the mirror hazard (provider-dependent partial-bar
   `iloc[-1]`). Both paths now use completed daily bars only.
2. **Weight tuning pooled the 5m and 15m streams** (plus 416 unlabeled
   pre-2026-07-27 rows) — the 5m noise floored the genuinely useful 15m
   ensemble. Weights are now tuned and loaded per stream
   (`{"15": {...}, "5": {...}}` schema; flat legacy files still load).
3. **Tuner rigor** — accuracy shrunk toward 50% by `TUNED_WEIGHT_SHRINKAGE`
   (50) pseudo-samples, weight movement capped at `TUNED_WEIGHT_MAX_STEP`
   (0.5) per retune, below-`min_samples` indicators hold their last tuned
   value (no snap-back to code defaults). Baselines + overlap surfaced in the
   tuner report, `!stats`, and the dashboard.
4. **SPY option projection bugs found in review, then the feature was removed
   entirely** — capture used the 9:30 open as spot, grading ran at 16:00
   (options trade to 16:15), and the promised catch-up didn't exist. Fixed,
   then removed same-day at the owner's request (SPX only).

## Fixed in the 2026-08-11 review round 2 (branch `claude/repo-review-u9qq0t`)

1. **Forecast grading asymmetry** — NEUTRAL/VOLATILE graded correct on flat
   windows but only ungraded on real moves: 101 free wins over the last 10
   result files (headline 63.4% vs 54.6% directional-only). Now symmetric.
2. **Swing option P&L** — the contemporaneous-requote branch was unreachable
   (grading is pre-open, lag always ≥ 1), so every outcome used the
   optimistic delta estimate; requote now accepted at lag ≤ 1. Grading made
   idempotent (no duplicate outcomes after a crash between writes).
3. **Restart recovery** — no more duplicate Daily Summary / Report Card after
   a late restart; open trades restored from levels now persisted in the
   fired `filters` row; swing done-flags persisted (no pre-open replay with a
   stale entry price mid-session; late first pass grades but doesn't scan).
4. **Ops** — nightly auto-commit scoped to `data/` only; push-after-commit
   failures reported honestly; `evaluate_bias` no longer rewrites bias files
   nightly on NEUTRAL days; EXPIRED trades excluded from the per-direction
   track record; `!stats` tolerates legacy outcomes without option P&L.

## Known data caveats

- `bias-*.jsonl` before 2026-08-06: old bias rule — don't judge the current
  vote by them.
- `results-*.json` before 2026-08-10 evening: graded under pre-fix pooled
  weights; rows before 2026-07-27 lack the `interval` label entirely and are
  excluded from tuning (reported as the "mixed" stream in analytics).
- `option_projections`/`option_outcomes` (2026-08-10 only): produced by the
  removed feature, capture-spot bug applies — historical record only.
- `results-*.json` graded before 2026-08-11 evening carry the old one-sided
  NEUTRAL/VOLATILE grades (free wins on flat windows, ungraded on moves) —
  headline accuracy isn't comparable across that boundary. Per-indicator
  numbers are unaffected (always directional-only).

## Open decisions / next steps

1. **Bias role decided 2026-08-11 (owner): direction gate removed.**
   Break-and-retest fires either side; NEUTRAL mornings still stand down;
   bias stays computed/logged/graded and still skews the day-forecast range.
   Watch item: LONG setups are no longer suppressed on non-BULLISH days and
   LONGs are 3/34 lifetime — the per-alert direction record is the guard.
2. **Confidence calibration** — map vote margin to empirical hit rate and
   re-derive `FORECAST_VOLATILE_CONFIDENCE` on the calibrated scale. Deferred
   because the gate and the scale must move together.
3. **De-correlate the ensemble** — drop/merge the weaker of EMA/RSI/VWAP
   after a `backtest_30m.py` check (changes live voting; not done unilaterally).
4. **Decide the 5m stream's role** — informational/VOLATILE-only vs.
   directional; it currently doesn't beat always-BULLISH.
5. **Let clean data accumulate** — the 15m stream has ~11 days of
   stream-labeled grades; distinguishing 55% from 50% needs ~250–300 windows
   per indicator (roughly 3–4 more weeks).
