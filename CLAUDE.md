# Repository guidance

## Product boundary

The owner's current requirement is **bullish, bearish and volatile SPX alerts, on completed candles, delivered privately through Discord**. Direction and volatility are separate observations, not option-entry recommendations. Read `README.md` for the operational contract and `docs/product-scope.md` for the scope decision. Do not restore a legacy mode, ORB background experiment, swing/forecast/flow notifications, auto-tuning or automatic Git synchronization.

## Source of truth and ownership

- `bot/core.py` defines actual cog registration. A tested module or old document does not mean a feature is enabled. During integration, inspect this file before claiming the new service is deployed.
- `market_state/` owns the pure versioned classifier, embeds and single Discord collector.
- `core/session_bars.py` and `core/state_store.py` own neutral bars, first-seen receipt semantics, migration and durable observations/delivery attempts. Never import historical ORB helpers into the runtime.
- `config.py` is the live configuration boundary. `research/config.py` is separate and must never be imported by startup. Historical settings cannot enable a live feature or break startup through parsing.
- `research/` contains explicit manual tools and historical readers only; see `research/README.md` before running one. No cogs, notification mode or background research scheduler belong there.
- `core/paths.py` anchors shared paths to the repository root; moving a module must not relocate evidence.

## Validation

```bash
python -m pytest -q tests --ignore=tests/historical  # live/shared contracts
python -m pytest -q                               # add research dependencies first
python -m compileall -q bot core market_state research tests
python -c "import main, bot.core, market_state.cog"
git diff --check
```

Use Python 3.12+ for the official Theta SDK. Offline synthetic tests do not establish live provider correctness, Discord delivery, Pi deployment, indicator profitability or options P&L. Separate historical-reader tests from live behavior tests; do not preserve obsolete tests by restoring retired cogs. Shared indicator/calendar changes can affect both suites.

## Data and side-effect safety

- Keep committed `data/` and licensed ignored `.thetadata-data/` intact. Never move licensed evidence into Git, public logs or an external AI service. Verify the private destination and applicable data permissions before publication.
- Preserve original audit dates, first-seen bars, receipt timestamps, event IDs and ambiguous delivery states. Migration failures must retain source evidence and fail closed.
- User-facing times are ET; stored timestamps are aware UTC. Use the exchange calendar for holidays/early closes.
- Provider clients are synchronous; runtime calls must retain single-flight ownership across thread cancellation. A process being alive does not prove data is fresh.
- Pure event rules need an explicit new version when semantics change. Do not relabel old observations or infer confidence/edge from indicator agreement.
- `research` does not mean network-free or read-only. Backfill, tuning, collection and dashboard tools may fetch or write. Invoke intentionally as `python -m research.<tool>` from the repository root. Do not automatically backtest or fetch paid data just because a PR changes code.
- Stale bytecode can obscure move-related import failures on Windows. `make clean` removes caches but preserves `.bot.pid` and data; it is not a stop command.

## Deployment

The Pi already has a clone. Changes must reach remote `master` before the owner can pull them there and run `make restart`, `make status`, `make tail`. Preserve its ignored `.env`; never request or print credentials. No systemd/Docker migration, subscription change or deployment is implied by a code change.

Historical experiments remain in `docs/strategy-log.md` and `docs/research/`, not as current feature instructions. Old duplicated agent workflow templates were removed because they referenced retired entry points and could trigger unrelated collection/tuning. Use this guidance and the current README instead.
