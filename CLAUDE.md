# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal Discord alert bot whose signals inform the owner's **manual trades** — it never executes orders. The goal is decision support: post a signal only when it carries information (change-driven posting, VOLATILE class for contradicting reads, per-direction track records on alerts), grade every prediction against actuals, and let the graded history drive what stays in the system. Strategy ideas are validated with backtests before going live and rejected with recorded evidence when they fail — see `docs/strategy-log.md` for what has been tried, adopted, and rejected.


Two features run side by side:

- **zero_dte/** — the intraday "First Candle Strategy": informational daily bias from the 50 SMA + opening gap + first 5-minute candle (demoted 2026-08-11 — it no longer gates entry direction; NEUTRAL mornings still stand down), break-and-retest entries either direction alerted with stop/2R target, intraday weighted-vote forecasts every `FORECAST_INTERVAL_MINUTES` (default 15) plus a 5-min companion stream (`FORECAST_5M_ENABLED`; directional calls under `FORECAST_VOLATILE_CONFIDENCE` post as VOLATILE). Every alert carries the ACTUAL session high/low tracked through the day (this replaced the ATR-projected morning day forecast, removed 2026-08-12). Every computed forecast is logged/graded, but posting is change-driven by default (`FORECAST_POST_MODE=change`): direction flip, confidence jump, new session high/low, or hourly heartbeat. A VIX spike watcher, plus a Bollinger band touch watcher (`BB_WATCH_ENABLED`, 1m+5m closes vs BB(20, 2.0), yfinance-only — no API budget, fires once per touch and re-arms after a close back inside), plus a candlestick pattern watcher (`CANDLE_WATCH_ENABLED`, named patterns on completed 1h + 1D candles via `core.indicators.detect_candlestick_patterns`, yfinance-only, observational — daily/weekly patterns backtested to no directional edge, 1h untested; hits logged to `candle_patterns` for a future 1h backtest); the standalone RSI watcher was removed 2026-08-12 (its reversal state lives on the forecast embed's RSI-caution line). There is no options coverage: the SPY ATM option projection was removed 2026-08-11 and the SPX 0DTE range feed 2026-08-12, both at the owner's request (historical `option_projections`/`option_outcomes`/`spx_options`/`day_forecast` files remain in `data/`; removed code is in git history).
- **swing/** — daily-timeframe weighted-vote signals on popular names (default TSLA/AMD/GOOG/META) suggesting 1–2 week option buys, with a specific 7–14 DTE contract picked from the yfinance chain. Scans twice daily (pre-open + midday confirmation of open signals).

Both features grade their own predictions against actuals and feed a self-tuning loop.

There is no test suite and no linter. Verify changes by running the relevant script (a backtest for strategy changes, the import/assembly smoke tests below for refactors).

## AI workflow commands

Slash-command prompt templates live in `.pi/prompts/*.md` (pi) and `.claude/commands/*.md` (Claude Code) — same content, different frontmatter comment. Use them instead of ad hoc requests when they fit:

| Command | Use for |
|---|---|
| `/initiate <description>` | Starting new work — env/git sanity check, figure out which area (zero_dte/swing/core/bot) is affected, ask clarifying questions before planning |
| `/backtest <what changed>` | Pick the right backtest script for a strategy change and summarize pass/fail |
| `/pr [description]` | Pre-PR checklist — smoke test, diagnostics, diff summary, draft commit message |
| `/tune [zero_dte\|swing\|both] [--write]` | Retune indicator weights and report before/after deltas |

## Commands

```bash
make install              # pip install -r requirements.txt
make run                  # run bot in foreground (python main.py)
make start / stop / status / restart   # background via .bot.pid (POSIX shell)
make logs / tail          # show / follow latest logs/bot-YYYY-MM-DD.log

python backtest_swing.py --start 2023-01-01 --sweep   # validate swing thresholds (bulk yfinance, no API budget)
python backtest_filters.py                    # replay fired+vetoed setups, per-filter expectancy audit (bulk yfinance)
python backtest_patterns.py --tickers SPY,QQQ --years 10   # daily candlestick patterns vs next-day direction (bulk yfinance; --weekly for weekly candles vs next-week)
python backfill.py 20 SPY QQQ                 # replay live 0DTE logic over history → data/ (--bias-report: grade the bias only, write nothing)
# (backtest.py / backtest_30m.py were deleted 2026-08-12 — both had drifted into
# stale forks with lookahead bias; there is currently NO honest replay harness for
# the intraday forecast ensemble or break-and-retest — build one before validating
# changes to either, and check docs/strategy-log.md first)
python dashboard.py 30 --open                 # HTML performance dashboard from data/
python tune_weights.py --days 20 --write      # retune 0DTE forecast weights → data/indicator_weights.json
python tune_swing_weights.py --write          # retune swing weights → data/swing_weights.json
python analyze_days.py                        # per-day accuracy summary

# quick smoke test after structural changes
python -c "import main, bot.core, zero_dte.cog, swing.cog, core.analytics"
```

Requires a `.env` (see `.env.example`): `DISCORD_TOKEN`, `ALERT_CHANNEL_ID`, optionally `TWELVEDATA_API_KEY` / `FINNHUB_API_KEY` (else yfinance only), `ZERO_DTE_ENABLED` / `SWING_ENABLED` toggles, `SWING_TICKERS`, `SWING_ALERT_CHANNEL_ID`.

## Architecture

Package layout with a strict dependency direction — `config`/`core.paths` ← `core.*` ← `zero_dte`/`swing` ← `bot`. Cogs never import `bot.core`; the shared `MarketDataClient` is constructor-injected. `core.analytics` is the one core module that reads feature trackers (its `swing.strategy` import is lazy/function-local).

- **`bot/`** — Discord shell. `bot/core.py::run_bot()` builds the bot and adds cogs per feature toggle. `bot/shared_cog.py` (SharedCog) owns `!stats !dashboard !commit !events` and a 60s housekeeping loop: econ briefing at market open, nightly weight retune + git auto-commit of `data/` at session close + `GIT_AUTOCOMMIT_DELAY_MINUTES`, and a restart notice (latest git change) posted once per process on ready. Housekeeping deliberately lives here so disabling a feature never stops history archiving.
- **`zero_dte/`** — `cog.py` (ZeroDTECog) runs the 60s `market_monitor` loop driving the trading day as a per-ticker `StrategyState` machine (premarket → first candle → ATR overextension → bias → break-and-retest until 11:00 ET → trade tracking → intraday forecasts → VIX/BB watchers → EOD evaluation). `strategy.py` has all trading logic + dataclasses; `tracker.py` persists/evaluates; `embeds.py`/`views.py` hold the Discord UI. Commands: `!bias !range !status !add !remove !watchlist !vix !forecast !test !menu`.
- **`swing/`** — `cog.py` (SwingCog) runs a 60s loop with **per-day done-flags** (never `tasks.loop(time=)` or minute-modulo — UTC/holiday-blind and double-fire prone). Pre-open pass (~9:00 ET): grade matured signals via the `data/swing_open.json` registry (self-heals after downtime), then scan each watchlist ticker (1 daily-candle call each; skip if a signal is open or in the 3-day cooldown) and post signals with a contract from `options.py`. Midday pass (12:30 ET): check open signals for target/invalidation. `strategy.py::evaluate_daily()` is pure (no network) — `backtest_swing.py` replays the exact live logic. Commands: `!swing !swingopen !swinglist !swingadd !swingremove`.
- **`core/`** — shared infra, no Discord, no feature logic. `market_data.py::MarketDataClient.fetch_candles()`: provider chain Twelve Data → Finnhub → yfinance with retry/backoff and rate limiting (`rate_limiter.py`). Returned frames always have UTC `timestamp` columns; all display logic converts to US/Eastern. `market_calendar.py` computes NYSE holidays/early closes locally — use it, never raw weekday/hour checks. `indicators.py` is the single home for indicator math (RSI, pivots, SMA/EMA/MACD/ATR/OBV) shared by both features. `tracking.py` has the generic per-day JSONL helpers. **`paths.py` is the single source of truth for filesystem paths — never derive paths from `__file__` in feature code.**
- **Root scripts** stay at the repo root (Makefile/muscle-memory): `main.py`, backtests, `backfill.py`, `dashboard.py`, tune scripts.

**Self-tuning loops** (parallel per feature): live signals append to `data/{kind}-YYYY-MM-DD.jsonl` → evaluation writes results (0DTE: nightly `results-*.json`; swing: `swing_outcomes-*.jsonl` after each signal's 1–2 week horizon) → `analytics.build_report()` powers `!stats`/`!dashboard`/`dashboard.py` → `tune_weights.py` / `tune_swing_weights.py --write` map per-indicator accuracy to weights → hot-reloaded live on file mtime change (no restart). The bot also runs this retune itself every night after close (`AUTO_TUNE_ENABLED`, in `bot/shared_cog.py` housekeeping, just before the git auto-commit).

**tasks.loop convention:** an unhandled exception kills a loop permanently — every loop has an `@loop.error` handler that sleeps 30s and restarts, and per-ticker work is wrapped in try/except.

## Conventions & gotchas

- `data/` is committed trading history — auto-committed after close ("Capture trading history" commits). Don't hand-edit or delete files there. Existing 0DTE file kinds (`forecasts`, `bias`, `outcomes`, `filters`, `votes`, `trade_decisions`, `results`, `bb_alerts`, `candle_patterns`) must keep their names; swing kinds are `swing_signals`/`swing_checks`/`swing_outcomes` + `swing_open.json` registry. (`option_projections`/`option_outcomes`/`spx_options`/`day_forecast` are retired kinds — historical files stay, nothing writes them anymore, don't reuse the names.)
- All user-facing times are ET strings; all stored timestamps are UTC ISO. Mixing these up is the most common source of bugs.
- Config lives in `config.py`, env-overridable (use the `_flag()` helper for booleans); add new knobs there rather than hardcoding.
- Strategy/tracker modules never import discord; embeds/views live in each feature's `embeds.py`/`views.py` and `bot/shared_cog.py`.
- yfinance is synchronous — always call it via `asyncio.to_thread(...)` from the bot (see `swing/options.py`, `core/vix.py`). Options chains: empty expiry lists, NaN/zero bid-ask outside market hours (→ `quote_stale`), and holiday weeks with no 7–14 DTE expiry must degrade gracefully, never abort a scan.
- `YFINANCE_PERIOD["D"]` must stay ≥ 2y — the swing evaluator needs a 300-bar daily window (SMA200 + 52-week high).
- On Windows, delete `__pycache__` dirs after moving files with git — stale bytecode masks import errors (`make clean`).
- `docs/robinhood-level2.md` is completed research (no code yet) for an optional Level II order-book signal via Robinhood Gold; decision: derived-signal-only, never post raw depth.
- **`docs/strategy-log.md` is the record of every strategy experiment** — adopted, adjusted, or rejected, with the backtest/live numbers and commits. Newest entries on top. **Update it whenever a strategy experiment concludes** (a signal/filter/threshold is adopted, retuned with evidence, or rejected). Check it before proposing a new signal idea — it may have already been tried and rejected (e.g. Dow reversal rule, prev-day-candle bias).
