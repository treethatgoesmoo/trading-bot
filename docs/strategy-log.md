# Strategy Log

A running record of every strategy idea, signal, filter, and tuning change we've tried — with the numbers that justified adopting, adjusting, or rejecting it. **Newest entries on top.** Backfilled from git history on 2026-08-06; keep it updated whenever a strategy experiment concludes (see CLAUDE.md conventions).

Entry format: **Status** (✅ adopted / ⚠️ adjusted / ❌ rejected), what was tried, the evidence, and the commit(s).

---

## Scoreboard (quick reference)

| Idea | Status | Key number |
|---|---|---|
| Daily bias: 2-of-3 vote (first candle + gap + 50 SMA) | ⚠️ revalidation pending | 65% replay number invalidated 2026-08-11 (lookahead in replay harness); live 5/6 since adoption |
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
