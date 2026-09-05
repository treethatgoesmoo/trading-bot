# Current Repository State — 2026-09-04

This is the current operational snapshot. The prioritized implementation and evidence roadmap is in [`next-steps.md`](next-steps.md). Historical decisions, rejected ideas, and measured results remain in [`strategy-log.md`](strategy-log.md). Detailed research protocols are under [`research/`](research/).

## Purpose and safety boundary

This repository is a personal Discord decision-support bot. It posts observations for manual review and **never executes orders**. Strategy changes are expected to be backtested, recorded, and graded after deployment. A working data integration is not evidence of predictive or profitable edge.

## Recent completed work

All four changes below are merged into `master`:

### PR #33 — intraday momentum validation and grading hardening

- Corrected the momentum-strength backtest to use absolute, same-session momentum and exact next-bar labels.
- Added frozen, checksum-backed SPY/QQQ/IWM/DIA research.
- Found no robust cross-asset next-bar continuation edge, so the new momentum and extension scores remain descriptive only.
- Added signal-time ATR to forecasts and schema-2 grading.
- Made analytics and weight tuning select one grading schema rather than pooling incompatible deadbands.

### PR #34 — simpler forecast display and timeframe context

- Removed visible confidence percentages, individual signal weights, indicator details, price, and day range from forecast posts.
- Removed RSI divergence from the live vote and extension score.
- Added plain-English momentum and stretch labels.
- Added latest completed 30-minute and one-hour candle directions as context only.
- Backtesting did not establish predictive edge for the higher-timeframe context, so it does not vote.

### PR #35 — ThetaData v3 foundation

- Added direct API-key authentication through the official ThetaData v3 client.
- Added normalized SPX/VIX snapshots, SPX index history, SPXW expiration discovery, narrow option snapshots, NBBO, Greeks, volume, and open interest.
- Kept ThetaData separate from the standard OHLCV fallback chain.
- Confirmed direct operation on the aarch64 Raspberry Pi with Python 3.13.5 and no Java.

### PR #36 — SPXW turning-point research and live paper shadow

- Added bounded historical SPXW trade/preceding-NBBO and exact-contract quote retrieval.
- Added an idempotent, checksum-verified collector for complete sessions.
- Added conservative aggressor inference, prior-known key levels, provisional turning-point states, and strict paper-contract selection.
- Added a separate, default-off `SPXFlowCog` for controlled Discord shadow testing.
- Added restart-safe 15- and 30-minute SPX and ask-to-bid contract outcome grading.
- A production Pi plumbing test collected 391 SPX minute bars and 100,997 deduplicated SPXW trade/quote rows. This validates access and normalization, not strategy edge.

## Runtime architecture

Dependency direction is:

```text
config / core.paths -> core -> zero_dte or swing -> bot
```

`bot/core.py` constructs one shared `MarketDataClient` and conditionally installs:

1. `SharedCog`
2. `ZeroDTECog` when `ZERO_DTE_ENABLED=1` (default on)
3. `SwingCog` when `SWING_ENABLED=1` (default on)
4. `SPXFlowCog` when `SPX_FLOW_ENABLED=1` and a ThetaData key exists (default off)

### Shared operations

`SharedCog` owns statistics, dashboards, economic-event output, nightly tuning, and automatic capture commits for tracked `data/`. Market scheduling uses the repository NYSE calendar, including holidays and early closes.

### SPY intraday system

The active forecast is a change-driven 15-minute SPY directional ensemble. It may output bullish, bearish, or volatile with an optional lean. Internal weighted aggregation remains active, but individual votes and weights are intentionally hidden from Discord.

Current internal inputs include RSI, EMA, VWAP, MACD, MACD momentum, volume, the legacy momentum voter, and VIX. RSI divergence is retired from live voting. The removed five-minute companion forecast is neither computed nor posted.

The forecast display contains:

- the 15-minute forecast;
- latest completed 30-minute and one-hour candle directions;
- plain-English momentum plus its raw descriptive score;
- plain-English stretch plus its raw descriptive score; and
- `Turning point: Not confirmed`.

The higher-timeframe candles, descriptive momentum, and stretch score are context—not probabilities, independent predictions, or reversal confirmation.

Two separately graded watcher streams remain:

- five-minute Bollinger-band touches; and
- five-minute relative-volume spikes.

Historical Bollinger results argue against blindly fading a touch. Volume results remain too sparse for a strong conclusion.

### Swing system

The swing cog scans daily candles for TSLA, AMD, GOOG, and META by default. It performs a pre-open scan and midday follow-up, maintains restart-safe open-signal state, grades matured outcomes, tunes separately, and may suggest a 7–14 DTE yfinance option contract. It remains independent of both SPY intraday forecasts and SPXW flow research.

## Experimental SPXW flow shadow

The SPXW feature is isolated, paper-only, and disabled by default:

```env
THETADATA_API_KEY=...
SPX_FLOW_ENABLED=1
```

It cannot place orders and does not feed the SPY forecast.

### Data roles

- ThetaData Indices Standard: current and historical SPX prices.
- ThetaData Options Standard: SPXW trades paired with preceding NBBO, snapshots, Greeks, and exact-contract quote history.
- Existing SPY OHLCV provider chain: current-session VWAP-extension context.

### Implemented live levels

- current-session open;
- previous-session high, low, and close, only when minute coverage is complete; and
- nearby 25-point round levels.

Weekly/monthly levels, SMA20/SMA200 context, and the broader volume framework described in the research design are not yet wired into the live cog.

### Provisional state rules

Internal states are `WAIT`, `POTENTIAL_TOP`, `TOP_CONFIRMED`, `POTENTIAL_BOTTOM`, `BOTTOM_CONFIRMED`, and `TREND_CONTINUATION`. “Confirmed” means only that a rejection or bounce matched the provisional rule; it does not mean the session's final high or low is known.

A potential setup currently requires:

- price actually reaches the watched level;
- approximately three SPY VWAP deviations of extension;
- sufficient elapsed-time SPX momentum;
- at least 25 classifiable ordinary OPRA prints;
- at least 20% classifiable flow; and
- at least 15% aligned directional-flow imbalance.

A reversal observation requires a matching potential episode followed by a rejection or bounce within ten minutes. Acceptance beyond the level is reported as continuation with explicit “do not fade” language. Only ordinary OPRA condition-code `0` prints contribute directional premium.

The `!spxflow` command reads the latest cached state and cannot mutate the automatic monitor.

### Paper contract policy

A contract is considered only after a reversal observation. It must satisfy all of the following:

- SPXW, same-day expiration, and direction-matching call or put;
- ask above zero and no more than $10.00 ($1,000 maximum debit at the 100× multiplier);
- positive displayed bid and sizes;
- fresh, non-crossed NBBO and fresh Greeks;
- spread no wider than 12%;
- absolute delta from 0.30 through 0.65; and
- enough session time for both outcome horizons.

Eligible contracts are ranked toward 0.50 absolute delta. Failure to find or fetch a contract produces `WAIT`; it does not suppress the market-state observation.

### Shadow grading and operations

Every automatically posted observation is persisted before Discord delivery and receives:

- 15-minute and 30-minute SPX movement;
- directional correctness;
- exact-contract bid exits where an executable matching quote exists;
- ask-to-bid paper return; and
- quote timestamp/delay or an explicit pending/missing state.

New observations stop early enough to finish both horizons before the close; prior observations continue grading. Provider calls run in worker threads, durations of 15 seconds or more are warned, and three consecutive failures pause polling for five minutes. A permanently hung SDK worker cannot be forcibly terminated, so live endpoint latency remains a monitored risk.

## Providers

### Standard OHLCV

`MarketDataClient` uses this fallback order:

1. Twelve Data
2. Finnhub
3. yfinance

Returned timestamps are normalized to UTC.

### ThetaData

`ThetaDataClient` is a separate synchronous adapter, not another fallback provider. It requires Python 3.12+ and `THETADATA_API_KEY`; no Theta Terminal or Java is required. Bot code must call it through a worker thread.

The production account currently has Options Standard and Indices Standard. SPXW is used for daily expirations; SPX identifies traditional monthly contracts and is not interchangeable.

## Storage and data handling

| Location | Tracked? | Contents |
|---|---:|---|
| `data/` | Yes | Bot history, evaluations, and learned weight files; may be auto-committed |
| `.research-data/` | No | Frozen Yahoo research snapshots and metadata |
| `.thetadata-data/` | No | Licensed SPX/SPXW research snapshots and derived shadow outcomes |
| `.env` | No | Credentials and deployment configuration |

Never hand-edit or delete committed `data/` history. Never place raw ThetaData/OPRA records in `data/`, logs, Discord, or Git. Raw live OPRA rows remain in memory; compact derived shadow records are local to the Pi under `.thetadata-data/shadow/`.

All stored timestamps use UTC ISO. User-facing times use US/Eastern. Filesystem paths are centralized in `core/paths.py`.

## Validation state

At the completion of PR #36:

```text
Full unittest suite:       37 passed
Python 3.12 focused suite: 26 passed
compileall:                passed
import smoke:              passed
git diff --check:          passed
```

Tests cover provider normalization, collector integrity, checksum/tamper detection, key-level completeness, OPRA filtering, provisional state transitions, contract selection, fallible contract lookup, executable exit-quote validation, and persisted 15/30-minute outcomes.

These checks establish implementation and data plumbing. They do **not** establish predictive accuracy, executable profitability, adequate sample size, or acceptable drawdown.

## Known limitations and next evidence gates

1. The SPXW turning-point hypothesis has no established edge and must remain paper-only.
2. The production collection proves one complete session of access, not a historical strategy result.
3. OPRA aggressor side is inferred from trade price versus preceding NBBO; it cannot reveal opening/closing intent, dealer inventory, multi-leg intent, or full-book liquidity.
4. The usable share of condition-code `0` prints and live request latency still need measurement across full sessions.
5. Weekly/monthly levels and some preregistered context fields are not implemented in the live watch.
6. SPY forecast grading measures underlying direction, not executable SPXW returns; only the new shadow stream begins exact-contract grading.
7. Licensed shadow data is local to the Pi, so retention and backup need an explicit compliant process.
8. Promotion beyond paper-only requires frozen chronological backtesting plus prospective shadow results, compared with price-only and always-directional baselines.

## Operator quick reference

```bash
# Run
python main.py

# Validate
python -m unittest discover -s tests -v
python -m compileall -q .
python -c "import main, bot.core, zero_dte.cog, zero_dte.flow_cog, swing.cog"

# ThetaData connectivity
python -m core.thetadata_client --symbol SPXW

# Complete-session licensed research collection (after close)
python collect_spxw_research.py --date YYYY-MM-DD

# Relevant frozen research
python backtest_momentum_extension.py --ticker SPY --interval 15
python backtest_timeframe_context.py
python analyze_session_thirds.py --stream 15
```

On the Raspberry Pi, deployment remains a repository pull followed by the owner's normal restart procedure. Secrets stay in the ignored `.env` and are not replaced by Git pulls.
