# Strategy Log

A running record of every strategy idea, signal, filter, and tuning change we've tried — with the numbers that justified adopting, adjusting, or rejecting it. **Newest entries on top.** Backfilled from git history on 2026-08-06; keep it updated whenever a strategy experiment concludes (see CLAUDE.md conventions).

Entry format: **Status** (✅ adopted / ⚠️ adjusted / ❌ rejected), what was tried, the evidence, and the commit(s).

---

## Scoreboard (quick reference)

| Idea | Status | Key number |
|---|---|---|
| 5m forecast stream as a posted alert | ⚠️ muted (still graded) | 51.4% directional (186/362) vs 55.4% always-BULLISH bar on 5m windows; posts removed 2026-08-12, stream keeps logging/grading |
| BB touch watcher 1m stream | ❌ dropped | ~15 ungraded pings/day (loudest stream in the channel, no evidence attached); 5m stream stays and is now graded (revert-to-middle vs extend) |
| Candlestick patterns → next-bar direction (daily AND weekly) | ❌ rejected both horizons | Daily: 48.3% (n=1066) vs 54.3% drift. Weekly, 25y × 4 ETFs: 52.4% (n=1283) vs 50.9% weighted bar; every "edge" flipped sign on out-of-sample DIA (Hammer +18.6p IWM / −11.2p DIA) |
| Daily bias: 2-of-3 vote (first candle + gap + 50 SMA) | ❌ demoted to informational | 50.9% (29/57) vs 52.6% always-BULLISH; direction gate removed from break-and-retest 2026-08-11; NEUTRAL still stands down; still logged/graded |
| Forecast weight tuning pooled 5m+15m streams | ⚠️ fixed to per-stream | Pooling floored real 15m signals (MACD 57.6% on 15m vs 50.5% on 5m); confidence still uncalibrated (90%+ stated → 53% realized) |
| Tuner statistical rigor: shrinkage + step cap + baselines + overlap audit | ✅ adopted | Always-BULLISH grades 55.4% on 5m — above every 5m indicator; EMA+RSI vote-agreement 100% on 15m (one signal counted 2-3×) |
| LONG break-and-retest entries | ❌ rejected (warned, not removed) | 3/34 wins, −21.8R lifetime; no exit geometry fixes it |
| Dow Theory reversal rule as swing voter | ❌ rejected | 50.5–51.3% at +10 bars vs 55% Structure voter, 60% constant-BULLISH baseline |
| VWAP as hard veto filter | ⚠️ demoted to advisory | Only filter that vetoed would-be winners; never correct as sole veto |
| VIX vote (original spec: stale 30m bars, fixed deadband) | ⚠️ rebuilt | 47% over 329 live windows → z-score gate on fresh 5m bars |
| Volatility deadband on forecast grading | ✅ adopted | Tuner was learning from ~$0.47 noise windows → everything at coin flip |
| Filter veto stack overall | ✅ validated | Vetoed setups would have averaged −0.45R |
| RVOL as sole veto | ✅ validated | 0/7 would-be winners, −0.92R avg — earning its keep |
| VOLATILE conflict guard (vote-count version) | ⚠️ recalibrated | Suppressed 60%-accurate forecasts, passed 0/6 → weight-ratio 0.75 guard (27% above vs 61% below) |
| RSI divergence inversion hack | ❌ removed as overfit | Fitted to old 5-bar endpoint compare; replaced with pivot detector at face value |
| Prev-day candle direction as bias | ❌ rejected | 48% (random) over backtest |
| Standalone RSI watcher pings | ⚠️ folded into forecast | Alert spam without decision value; divergence now a forecast voter |
| MACD momentum + candle direction as forecast voters | ⚠️ downweighted to noise floor | 48.9% / 48.8% (coin flip) over 55-day backtest |

---

## 2026-08-12 — Alert diet + grade-everything pass

**Status:** ⚠️ two streams demoted on evidence; every remaining ping stream now grades

Audit of a typical day's channel volume (~25–30 posts) against the graded record, owner decisions:

- **5m forecast stream muted** (posts removed, computing/logging/grading unchanged). Over all interval-labeled graded bars: **51.4% directional (186/362) vs the 55.4% always-BULLISH baseline** on 5m windows — every post was statistically a shrug. The 15m stream (60.7%, 74/122) keeps posting. The 5m stream's open question from the per-stream tuning entry stays open; its record keeps accumulating and can argue it back in.
- **BB watcher 1m stream dropped.** ~15 pings/day average (19 max) — the loudest stream in the channel — with zero grading attached. The 5m stream stays, and BB touches are now **graded nightly** (`tracker.evaluate_bb_alerts`, in-place on `bb_alerts` rows): REVERTED (a later 5m close reaches the alert's middle band first), EXTENDED (a close a full band-width beyond the band first), DRIFT at session end graded by which side of the touch price it closed on — symmetric, no free passes.
- **New house rule: every alert stream must grade.** An ungraded ping can never earn or lose its place. Added nightly graders (all idempotent, in-place, with startup catch-up):
  - **VIX pings** (`evaluate_vix_alerts`): stored SPY-implied direction vs SPY's next 30 min, forecast-style ATR deadband (FLAT → recorded ungradeable, not a free win).
  - **Candlestick patterns** (`evaluate_candle_patterns`): directional patterns vs the next archived candle's close-vs-open (the backtest_patterns rule); Doji/Inside Bar as chop calls vs the trailing-20 median |move| once the `candles` archive holds 10 priors. Daily patterns lag one grading day by construction (next candle archives the following morning).
- **Kept as-is (owner):** candlestick pattern posts on both timeframes — the daily horizon is rejected as a signal but the posts are explicitly observational, and the 1h horizon now builds the graded record that will settle it.
- Grading gap to watch: none of these graders backfill history beyond the yfinance intraday window (~60d for 5m bars), so old `bb_alerts`/`vix_alerts` rows past the window stay ungraded rather than being guessed at.

## 2026-08-12 — Declutter: day forecast + SPX range feed removed; actual session H/L on every alert

**Status:** ❌ removed at owner's request (noise reduction; no strategy logic changed)

- **Morning day forecast removed** (direction + ATR-projected high/low, plus its embed, recovery path, and `day_forecast` data kind — retired). Its direction was the demoted no-edge bias; its projected range was an estimate nobody could act on. Replaced by the **actual session high/low**, computed from data already fetched (no new API calls), tracked live in `StrategyState` and shown on every forecast/signal/filtered alert, the daily summary, and `!status`. The change-driven posting trigger "projected-range breach" became "new session high/low" (0.1% hysteresis).
- **SPX 0DTE range feed removed** (`zero_dte/spx_options.py`, its loop, `!spxrange`, `SPX_OPTIONS_*` config; `spx_options` kind retired). With the SPY projection already gone 2026-08-11, the bot now has no options coverage — signals are underlying-price alerts informing manual SPX trades. Removed code is in git history (`d20d240`..this commit) if wanted back.
- The bias embed stays (informational, graded nightly, drives the NEUTRAL stand-down) — decided 2026-08-11.
- **Round 2 (same day): dead/misleading code deleted.** `backtest.py` + `backtest_30m.py` (both stale forks — oracle bias in the daily-bias replay, phantom indicators/wrong interval in the forecast one; any number they printed was misinformation; there is now NO harness for the intraday ensemble or break-and-retest until an honest as-of replay is built) and the standalone RSI watcher + `!rsi` (~300 lines, pings already default-off and folded into the forecast's RSI-caution line; `rsi_alerts` kind retired). Kept on evidence/owner choice: the 5m forecast stream (still graded — its role is an open question) and the econ-events briefing.

## 2026-08-12 — Weekly candlestick patterns: rejected after out-of-sample split

**Status:** ❌ rejected (closes the candlestick question at both horizons)

- Follow-up to the daily rejection: same detectors resampled to completed weekly candles, graded vs next-week close-vs-open, 25y × SPY/QQQ/IWM/DIA (~1,244 weeks each; per-ticker tables + combined). Pre-committed bar: positive edge holding its **sign on every ticker**, with avg-move agreeing with hit rate.
- Initial SPY+QQQ pool looked suggestive (ALL 54.2%, Hammer +9.5p, engulfings positive) — the four-ticker split showed **QQQ was carrying the pool** (55.7% all-directional) while never-touched **DIA came back 48.8% with nearly every pattern negative**. Sign flips everywhere: Hammer +14.4p QQQ / +18.6p IWM / **−11.2p DIA**; Bullish Engulfing +8.0p QQQ / **−2.1p DIA**. Combined ALL directional 52.4% (n=1283) vs a 50.9% direction-weighted bar ≈ 1.1σ.
- **Bearish Engulfing confirmed the accuracy-without-expectancy trap**: +4.9p combined hit rate but −0.04% combined avg move (market flat-to-up after the pattern on SPY/QQQ) — right slightly more often, wrong bigger.
- Indecision reads: Doji 49.4% / Inside Bar 46.5% combined at predicting a quiet week — no chop signal either.
- **Candlesticks are now closed at both tested horizons** (daily next-day, weekly next-week) for index ETFs: explicit geometry, prior-move context conditioning, drift-honest baselines, out-of-sample tickers — no standalone directional edge survives. Detectors (`core/indicators.py`) and harness (`backtest_patterns.py`, `--weekly`) remain for any future variant (different horizon/conditioning/single names), which must clear the same bar.

## 2026-08-11 — 2-of-3 bias vote revalidated honestly: no edge over always-BULLISH

**Status:** ❌ validation failed → **direction gate removed same day (owner decision)**

- First honest measurement of the current bias rule, via the new report-only replay (`python backfill.py 60 SPY --bias-report` — fixed harness, strict `< day` truncation, nothing written): **29/57 directional calls correct (50.9%) vs 52.6% always-BULLISH over the same days** (2026-05-15 → 2026-08-11, 3 NEUTRAL days). Bullish calls 20/38 = 52.6% — exactly the drift rate, i.e. zero information; bearish calls **9/19 = 47.4%**.
- The **65% replay that justified adopting the vote on 2026-08-06 is confirmed to have been lookahead artifact** (the harness fed the replay day's own close to the trend/RSI components — logged 2026-08-11). The 5/6 live record since adoption was small-sample luck.
- The bearish leg is now weak across **both** bias rules and both measurement eras: 38.5% live under the old first-candle rule, 47.4% replayed under the 2-of-3 vote. Whatever the morning information set contains, "today will be red" has never been extractable from it here.
- Statistical honesty: n=57 → ±13p at 95%, so this does not prove the vote is worse than always-BULLISH — but the adoption evidence is gone and nothing measurable remains in its favor. Third strike for morning-direction signals overall (old bias rule 50%, daily candlestick patterns 48.3%, 2-of-3 vote 50.9% vs 52.6% bar).
- **Decision (owner): break-and-retest no longer gates entry direction on the bias.** Either side may fire on a break-and-retest; the filter stack is the quality gate; the per-direction lifetime record + below-breakeven warning on each alert (the 2026-08-06 precedent) is the honest decision-time signal. **NEUTRAL mornings still stand down entirely** — that rule is about a conflicted signal set, not direction. The bias itself keeps computing/logging/grading (evidence accumulates; the embed footer now states it's informational and shows the graded number), and the day-forecast ATR skew still uses it. Watch item: gate removal un-suppresses LONG setups on non-BULLISH days — LONGs are 3/34 lifetime, so the direction record warning matters more now.

## 2026-08-11 — Daily candlestick patterns as a bias/forecast source: rejected

**Status:** ❌ rejected (backtest evidence; nothing wired live)

- Idea (owner, from multi-timeframe pattern-trading doctrine): read named candlestick patterns off SPY's completed daily candles each morning, use the pattern's direction as (or alongside) the day forecast, confirm on 1-4h, then refine intraday. Detectors + as-of replay built first per house rule (`core/indicators.py::detect_candlestick_patterns`, `backtest_patterns.py` — strict `< day` slicing, graded close-vs-open like `evaluate_bias`, baselined against each direction's unconditional drift, not 50%).
- 10y SPY+QQQ (4,904 graded days, up-day drift 54.3%): **all directional patterns 48.3% (n=1066)** — at or below drift nearly everywhere. High-n patterns: Bullish Marubozu −0.9p (n=236), Bearish Engulfing −1.8p (n=182), Bearish Marubozu **−6.9p** (n=170), Bullish Engulfing −1.6p (n=150), Hammer −4.3p (n=98). Best result Shooting Star **+6.8p at n=80 ≈ 1.2σ** (noise needs ~±11p at that n to clear 95%) — and the same pattern "won" +16.1p on a pure random-walk synthetic run, best-of-8 selection doing the work. Trend-conditioned splits: no cell both significant and consistent across tickers' pooled run.
- Indecision reads failed too: Doji 47.4% (n=498) / Inside Bar 48.1% (n=507) at predicting a below-median-range next day — no chop signal for the VOLATILE/deadband layer.
- Bearish Marubozu's below-drift read (market tends to bounce after a full-body red day) is a mild mean-reversion echo, but inverting a fitted-to-history pattern is the exact overfit path the removed RSI-divergence inversion documented — declined.
- Third independent no-signal result for daily candle shapes here (prev-day candle bias 48%; candle-direction voter 48.8%). Multi-timeframe confirmation layers (steps 2-3 of the idea) are moot without a step-1 signal to confirm. Detectors and the harness stay in the repo — reusable if a pattern idea returns at a different horizon or conditioning.

## 2026-08-11 — Review round 2: grading asymmetry, swing P&L pricing, restart recovery

**Status:** ⚠️ bugs fixed (measurement + recovery; no vote/threshold logic changed)

- **NEUTRAL/VOLATILE forecasts had a one-sided report card**: correct on a flat window but merely *ungraded* on a real move. Over the last 10 `results-*.json`, 101 such free wins inflated headline accuracy to **63.4% vs 54.6% directional-only**. Grading is now symmetric (wrong on a real move). The weight tuner was never affected (it only counts directional actuals) — this was the human-facing number that drives keep/kill decisions. **Results files graded before this fix keep the old asymmetric grades** (re-grading history needs market data); accuracy across the boundary isn't comparable.
- **Swing option P&L was always the optimistic delta estimate**: grading runs pre-open, so `lag_days` is always ≥ 1 and the "contemporaneous requote" branch was unreachable. A next-morning requote returns the prior session's close — exactly the resolution-day exit quote wanted — so the branch now accepts lag ≤ 1. Grading is also idempotent now (a crash between the outcome append and the registry removal no longer double-counts a signal into analytics/tuner).
- **EXPIRED 0DTE trades no longer count as losses** in the per-direction track record shown on signal alerts (they're unresolved, not losses; was latent — no EXPIRED rows exist yet).
- **`evaluate_bias` re-rewrote history forever on NEUTRAL days** (its idempotence guard keyed on `correct`, which NEUTRAL leaves `None` permanently) — now keys on `actual_direction`.
- **Restart recovery** (the rehydration half that was missing): a restart after 11:00/16:00 ET no longer re-posts a degraded Daily Summary / Forecast Report Card; fired signals now persist their levels in the `filters` row (additive key) so an open trade survives a restart and still grades instead of vanishing; swing pre-open/midday done-flags persist to `.swing_daily_state.json` so a mid-session restart can't replay the pre-open pass, and a genuinely-late first pass (long outage) grades but skips the scan rather than posting an entry priced off a session-stale close.
- Ops: the nightly auto-commit is now pathspec-scoped (`git commit -- data`) so staged dev work can't be swept into a "Capture trading history" commit; a push failure after a successful commit reports "committed locally" instead of "nothing committed"; provider-agnostic `!stats` no longer crashes on a legacy outcome whose `option` dict lacks `pnl_pct`.

## 2026-08-11 — SPY option projection/grading removed (owner trades SPX only)

**Status:** ❌ removed at owner's request

- The 0DTE ATM option projection + EOD grading (added 2026-08-07, PR #27) quoted **SPY** contracts, but SPX is the traded instrument — the projections were decision noise, and the graded dataset would have tuned a model for options the owner doesn't trade. Removed: `zero_dte/options0dte.py`, `core/options_math.py` (BS pricing, no other consumer), the cog wiring/embed/tracker functions, and the `OPTION_PROJECTION_*` config. SPX options coverage continues via the `zero_dte/spx_options.py` range feed.
- Only two days ever graded (2026-08-10, both flagged suspect from the capture-spot bug fixed the same day). Historical `option_projections-*`/`option_outcomes-*` files stay in `data/` untouched; the kind names are retired, not to be reused. If an option projection is ever wanted again, build it on SPX quotes — the removed code is in git history (`d20d240`..`090ae0e`).

## 2026-08-11 — Full-repo review: option-projection grading fixes; tuner snap-back bug

**Status:** ⚠️ bugs fixed (feature logic unchanged)

- **Option projection captured "spot" as the 9:30 open**, not the price when the projection actually runs (~9:35+). A drifted spot picks the wrong ATM strike and poisons the IV calibration — a mid embedding real intrinsic solved against a spot with zero intrinsic inflates sigma, or falls outside the model's band and silently drops to stale chain IV. Now fetches the live 1m close at capture (open stays as fallback). **The 2026-08-10 `option_projections`/`option_outcomes` rows predate this fix (and were quoted off stale bid/ask at 9:35) — treat them as suspect when tuning.**
- **Option grading ran at 16:00**, but SPY/QQQ options trade until 16:15 ET and the contract's Yahoo daily bar needs settling time — actuals were captured off an incomplete bar. Grading now waits until session close + 30 min on its own done-flag.
- **The "retried by the next catch-up pass" for ungraded projections didn't exist** — `_grade_missed_days` never called option grading, and the fetch took Yahoo's latest bar (wrong day after downtime). Catch-up now retries past dates with ungraded contracts, and the fetch selects the bar dated to the contract's expiry.
- **Tuner snap-back**: an indicator dipping below `min_samples` reverted to its *code default* instead of holding its last tuned value — a 0.3→1.0 jump that bypassed the brand-new step cap. Both tuners now anchor to the previously written file.

## 2026-08-11 — Tuner made noise-resistant; naive baselines and overlap audit added

**Status:** ✅ adopted (measurement changes; no vote logic touched)

- **Shrinkage + step cap** (`core/tuning.py`, both tuners): measured accuracy is pulled toward 50% by `TUNED_WEIGHT_SHRINKAGE` (50) coin-flip pseudo-samples before the `(acc−0.50)×20` mapping, and a weight moves at most `TUNED_WEIGHT_MAX_STEP` (0.5) per retune. Rationale: at n=20 a coin-flip indicator routinely measures 65%, and the old mapping would hand it weight 3.0 overnight — the swing file had **Cross at weight 2.0 off 4 graded signals at 25% accuracy**. A real edge still reaches full weight in about a week of retunes; a noise spike doesn't.
- **Naive baselines** (tuner report, `!stats`, dashboard): always-BULLISH and same-day persistence, per stream. First read: **always-BULLISH grades 55.4% on the 5m stream (n=491) — at or above every 5m indicator including RSI (55.7%)**, i.e. the 5m ensemble currently has no measurable edge over "just say bullish"; on 15m the bar is 53.8% and RSI (61.3%) / VWAP (58.9%) / MACD (57.6%) clear it. The 5m stream's directional value is now an open question (echoes the old 30m constant-BULLISH lesson from the Dow-rule test).
- **Overlap audit** (`analytics.indicator_overlap`, dashboard card, tuner report, flagged ≥85%): pairwise directional-vote agreement per stream. First read: **EMA+RSI agree 100.0%** (15m, n=113) and 99.7% (5m); **RSI+VWAP 96.5–99.5%**; EMA+VWAP 86.5% (5m) — RSI, EMA, and VWAP are one trend signal voted three times, which inflates the weighted margin (and thus stated confidence and the VOLATILE guard's inputs). RSI is effectively "EMA, but only when stretched" — its abstention band is where its extra accuracy lives. Candidate action once samples grow: drop/merge the weaker of each flagged pair; **not** applied yet — dropping voters changes live behavior and should be backtested (`backtest_30m.py`) first.

## 2026-08-11 — Weight tuning was pooling the 5m and 15m streams; split per-stream

**Status:** ⚠️ measurement fixed — confidence calibration still open

- Investigating "the weighted indicators are very misleading": the tuner (and `!stats`/dashboard indicator accuracy) pooled every graded window — 746 5m windows, 248 15m windows, and 416 pre-2026-07-27 rows with no interval label (an inseparable mix, since the 5m stream launched 2026-07-08 but grading only started recording `interval` on 2026-07-27). The much larger 5m sample dominated, and most indicators are coin flips on 5m windows — so the nightly retune floored nearly everything (all 0.3 except RSI 1.1) and applied that one flattened set to *both* streams.
- Split per-stream, the 15m ensemble is real: **RSI 61.3% (n=75), VWAP 58.9% (n=158), MACD 57.6% (n=158), Momentum 56.3% (n=158)** — vs 55.7/52.3/50.5/50.3% on 5m. EMA, Divergence, and VIX are ≤51% on both streams (floored on evidence, fairly this time).
- Fix: `tune_weights.py` now grades each stream separately (unlabeled legacy rows excluded) and writes a per-interval weights schema; `strategy._weight_overrides(interval)` loads the set matching the stream being forecast (flat legacy files still apply everywhere); `!stats` and the dashboard label indicator accuracy per stream (`RSI [15m]`) instead of pooling. Weights regenerated: the 15m stream recovered a genuine ensemble (RSI 2.3, VWAP 1.8, MACD 1.5, Momentum 1.3) while 5m stays floored.
- **Still open — confidence is uncalibrated:** over 810 graded directional calls, stated confidence 70–80% hit 53.8%, 80–90% hit 53.1%, and 90–100% hit **53.4%**. The margin→confidence mapping (50 + 50×margin) has no empirical anchor, and `FORECAST_VOLATILE_CONFIDENCE=65` gates on these inflated numbers, so recalibrating the scale means retuning the gate with it — deferred as a deliberate decision rather than bundled here. Until then, read posted confidence as vote margin, not win probability (the dashboard's calibration card shows the honest mapping).

## 2026-08-11 — Daily-bias replay harness had lookahead; 65% validation number invalidated

**Status:** ⚠️ harness fixed, revalidation pending

- Investigating "the daily forecast seems to be a coin flip": the live graded record is **18/32 = 56%** overall (June–July, under the old first-candle-wins rule: exactly 50%; bearish calls 5/13 = 38.5%), and only 6 graded days (5/6) exist under the 2-of-3 vote adopted 2026-08-06 — so most of what the channel shows *is* the old coin-flip rule.
- Worse: the **65% vs 50%** replay that justified the 2-of-3 vote was contaminated. `backfill.py` truncated the daily frame with `date <= d`, so `get_daily_bias` read the replay day's **completed bar** as its latest row at 9:35 AM — the 50 SMA trend vote compared the day's own close against the SMA, and the RSI ≥70/≤30 contrarian override used end-of-day RSI, while grading is close-vs-open. The trend/RSI components were peeking at the answer.
- Live had the mirror-image hazard: `get_daily_bias` used `iloc[-1]`/`iloc[-2]` raw, so its meaning depended on whether the provider includes today's in-progress daily bar (included → trend used the partial 9:35 close; omitted → gap and pivots were computed off the day-before-yesterday). `compute_day_forecast` already filtered to completed days; the bias didn't.
- Fix: `get_daily_bias` now drops any bar dated today (ET) and reads all of trend/gap-base/pivots/RSI/multi-day from the last **completed** session; backfill truncates strictly (`date < d`). Live and replay now see identical information. The honest accuracy of the 2-of-3 vote is unknown until the 60-day replay is re-run (`python backfill.py 60 SPY` + `python analyze_days.py` — needs network access to yfinance). **Lesson: an as-of replay must slice with a strict inequality — `<=` on the decision day hands the harness the label it is predicting.**

## 2026-08-06 — Daily bias reworked to 2-of-3 vote; LONG side of break-and-retest flagged

**Status:** ✅ bias adopted · ❌ LONG entries rejected (kept with a warning) — `1c6e454`

- The old bias let the first 5-min candle win unconditionally — the "gap confirms" branches were dead code. It graded **50% over 60 replayed days** (coin flip); bearish calls were **33% live**. New system: first candle, gap, and 50 SMA trend vote; majority wins; RSI ≥70/≤30 contrarian override kept; full three-way conflict → NEUTRAL (no entries, 50/50 ATR day-forecast split). Backfill replay: **65% vs 50%**, bearish precision **5/8 vs 4/12**.
- Replaying all 62 recorded setups: **LONG break-and-retest won 3/34 (−21.8R)** while shorts ran ~breakeven. No stop/target geometry change fixes it. Rather than delete the side, signal alerts now show per-direction lifetime track record (wins/total, net R) with a below-breakeven warning — the weak side is visible at decision time.

## 2026-08-06 — Swing P&L priced at resolution; VWAP demoted to advisory

**Status:** ⚠️ adjusted — `3a82b79`

- Swing option P&L is now priced as of signal resolution (was misleading otherwise).
- VWAP filter demoted from hard veto to advisory: the 2026-07-27 filter expectancy audit showed VWAP was the only filter vetoing would-be winners and was never correct as the sole vetoing filter.

## 2026-08-02 — Dow Theory reversal rule: rejected as a swing voter

**Status:** ❌ rejected — `38227db` (harness), `7db3723` (verdict)

- Implemented the strict failure-swing trend-change rule (`core.indicators.dow_trend`, walk-forward, no lookahead) with a dedicated applicability harness (`backtest_dow.py`) before wiring anything live.
- Event accuracy at +10 bars: **50.5–51.3%** across three samples (2021/6 tickers, 2023/swing tickers, 2015/12 tickers) — below the existing Structure voter (**55.0%**) and ~10 points below a constant-BULLISH baseline (**60.0%**). Below coin flip at +5 bars. Sample size and redundancy checks passed — a real negative result, not a thin-sample artifact.
- Never wired into live voting. Kept as a reproducible negative result. **Lesson: check the applicability harness against a naive baseline before adoption — a rule can be "real" and still worse than doing nothing.**

## 2026-07-27 — VIX vote rebuilt; forecast posting made change-driven

**Status:** ⚠️ rebuilt — `44d2022`

- The original VIX vote graded **47% over 329 live windows**: it compared a partially-formed 30-min bar against one from 60–90 min earlier with a fixed 0.3% deadband (~0.05 VIX pts), voting nearly every window on stale noise. Rebuilt on fresh 5-min bars over a ~30-min window; the move must clear 1.0 std devs of the day's own 30-min moves to vote at all. Literature flags VIX as the strongest short-horizon SPY predictor — the spec deserved the care.
- Posting switched to change-driven (`FORECAST_POST_MODE=change`): post only on direction flip, confidence jump, projected-range breach, or hourly heartbeat. Every forecast still logged/graded.

## 2026-07-27 — Volatility deadband grading; filter veto stack validated

**Status:** ✅ adopted — `70bc1cf`

- Forecast windows whose |move| is under 0.25 × interval ATR(14) now grade as FLAT: directional calls go ungraded (noise, not a miss). **The tuner had been learning from force-labeled ~$0.47 windows that put every indicator at coin flip.** Nightly retune now uses a rolling 30-day window so pre-deadband labels age out.
- First per-filter expectancy audit (`backtest_filters.py`, meta-labeling style): the veto stack earns its keep — **vetoed setups would have averaged −0.45R**; RVOL sole vetoes went **0/7 (−0.92R avg)**; only VWAP vetoed would-be winners (→ demoted 2026-08-06).

## 2026-07-10 — VOLATILE conflict guard recalibrated

**Status:** ⚠️ adjusted — `1088a1b`

- After the nightly retune concentrated weight in RSI/MACD/Divergence, the old conflict guard (≥2 opposing votes at ≥0.5 weight ratio) fired on most mixed tapes — everything came out VOLATILE. Rescoring graded history with current weights showed the guard had it **backwards**: forecasts it suppressed graded **60%**, while lone-heavyweight conflicts it let through graded **0/6**. Replaced with a weight-ratio-only guard at 0.75 (**27% accuracy above the ratio vs 61% below**). VOLATILE forecasts now show the winning side as a lean instead of hiding the read.

## 2026-07-08 — VOLATILE forecast class; 5-min stream; alert spam fixes

**Status:** ✅ adopted — `1f0d195`, `9c939c1`, `09eb868`

- Contradicting signals get a yellow VOLATILE caution instead of a forced direction; later gated at 65% confidence (`FORECAST_VOLATILE_CONFIDENCE`). 5-min companion forecast stream added, graded per-interval.
- Alert spam fixed structurally: RSI cooldowns and relevance gates, VIX hysteresis, batched pings. **Lesson: alert channels lose value fast when signals re-fire on every poll — dedup on stable event keys (pivot timestamps), not window indices** (`ae27e2f` fixed the same class of bug earlier: a lingering divergence fired ~19× across 19 polls, now 1×).

## 2026-07-06 — RSI divergence folded into forecast; standalone pings retired

**Status:** ⚠️ consolidated — `6acec72`, `a747770`

- RSI divergence became a forecast voter; tuned weights got a floor so retunes can't zero an indicator out permanently. Standalone RSI watcher pings turned off — caution now rides the forecast embed instead of separate alerts. Entry-timing guidance added to option-buy alerts (`45fcced`).

## 2026-07-01 — Forecast calibration fixes; divergence inversion hack removed

**Status:** ⚠️ adjusted — `0001aec`

- Confidence became margin-based over **all** reporting indicators (NEUTRAL votes count in the denominator) — a couple of lone agreeing signals could previously print 100% while the ensemble abstained.
- The divergence **inversion hack** (see 2026-06-06) was removed as overfit: it was fitted to the old 5-bar endpoint compare. Replaced with the shared pivot-based detector voted at face value, weight cut 3.0 → 1.5. **Lesson: when a signal grades worse than random, first suspect the signal's implementation, not the market — inverting a badly-measured signal bakes the measurement bug in.**
- Also: daily RSI filter demoted from silent veto to warning (was blocking trades contrary to documented intent); NYSE calendar added so holidays/half-days stop producing garbage grades.

## 2026-06-24 — VIX added as a forecast signal

**Status:** ✅ adopted (later rebuilt 2026-07-27) — `09c8b46`

- VIX moves inversely to SPY: rising VIX → bearish vote, falling → bullish, weight 2.0. Live only — skipped in backfill replay since historical VIX can't be aligned. Regime-crossing and spike alerts added. (Original spec graded 47% and was rebuilt — see 2026-07-27.)

## 2026-06-22 — Performance tracking infrastructure

**Status:** ✅ adopted — `e9a4c48`

- The foundation for everything above: per-indicator accuracy tracking, confidence calibration, bias grading vs actual daily move, trade win-rate/R, filter block-rates → `data/*.jsonl` + analytics + dashboard. **Every adopt/reject decision in this log depends on this loop existing.**

## 2026-06-06 — First data-driven retune; bias source changed

**Status:** ⚠️ adjusted — `03e7e6c`, `68ee495`, `79f8efa`

- 55-day backtest graded the indicators: Divergence **44.4%** (worse than random — inverted at the time, later found to be a measurement bug), MACD_Mom **48.9%** (noise, 1.5 → 0.5), candle direction **48.8%** (noise, → 0). Result: 48.8% → 51.4% overall, 23.4% → 31.7% on reversals.
- Prev-day candle direction as the bias source graded **48% (random)** → replaced with first-candle direction + gap confirmation + RSI contrarian flip. (Itself replaced by the 2-of-3 vote on 2026-08-06 when the "gap confirms" branches turned out to be dead code.)

## 2026-06-03 — Filter thresholds tuned from live logs

**Status:** ⚠️ adjusted — `8616f0a`

- ATR overextension multiplier 2.0 → 3.0: a $0.84 range on a $750 stock (0.11%) was being flagged as overextended, skipping valid days.
- RVOL threshold 1.5 → 1.2: morning volume naturally declines after the open, so breakout candles rarely hit 1.5×; nearly every setup was being filtered.

## 2026-05-28 — Origin: First Candle Strategy

**Status:** ✅ baseline — `50487a9`

- Initial premise: daily bias from the 50 SMA + opening gap + first 5-minute candle; break-and-retest entries with stop at first-candle range and 2R target. Everything above is iteration on grading this premise against reality.
