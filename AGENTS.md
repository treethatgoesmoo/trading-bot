# Trading bot guidance

## Product and ownership

One private Discord service: **SPY breakout/retest signals for manual SPX 0DTE execution**. Read `README.md` and `docs/strategy.md`. Rules are unvalidated; never fabricate confidence, fills, SPX/SPY conversion, option returns or contract recommendations.

- `bot/core.py` is the cog-registration source of truth; only `SpySetupCog` belongs in startup.
- `spy_setup/` owns pure versioned rules, rendering and collection.
- `core/session_bars.py` owns first-seen SPY OHLCV; `core/state_store.py` owns the new `.thetadata-data/spy-setups` ledger and delivery intents.
- Lifecycle identity and event time drive deduplication/freshness, not direction or latest candle time. Never replay historical events into Discord.
- `config.py` is the only live settings boundary. No legacy aliases, strategy modes, research scheduler, auto-tuning, dashboard or automatic Git operations.
- Remove obsolete callers/tests/docs with retired code; do not retain dead APIs to satisfy obsolete tests.

## Safety

Preserve `.env`, committed `data/`, ignored provider evidence, logs and original audit dates. Do not run paid collection, contact Discord, change subscriptions, delete evidence or deploy merely to validate code. No credentials/raw licensed data in Git or public logs.

Keep collection/publication opt-ins, private-channel checks, immutable receipt times, atomic evidence writes, fail-closed data handling and ambiguous-send suppression. Synchronous provider workers must retain single-flight/process ownership through cancellation. Never delete a lock file to bypass its owner. New rule semantics require a new version; old state must not be relabeled.

SPY `nqb` volume/VWAP is feed-specific; verify entitlement and historical/current comparability independently. User-facing times are ET, storage UTC; respect exchange holidays and early closes.

## Validation

```bash
python -m pytest -q tests
python -m compileall -q bot core spy_setup tests
python -c "import main, bot.core, spy_setup.cog"
git diff --check
```

Use Python 3.12+ for the official SDK. Offline tests do not prove live feed semantics, Discord delivery, trading profitability or SPX option P&L. Do not claim deployment from local changes. The existing Pi must stop the old process before pulling reviewed/merged `master` and restarting; preserve its `.env` and data.
