# SPY setups → SPX 0DTE

A private Discord service watching **SPY price structure, VWAP and relative volume** for opening-range breakout/retest setups. Execution is manual in **SPX 0DTE options**. No broker orders, contract selection, premium targets, confidence score or validated trading edge.

## What it does

- Builds SPY's first 15-minute regular-session range from completed 1-minute OHLCV candles.
- Requires a breakout aligned with session VWAP and at least 1.2× matching-minute relative volume, then a later retest and a later continuation close.
- Publishes distinct lifecycle transitions: forming (breakout/retest), triggered, invalidated, expired or target reached. **Forming is not an entry.** Idle/unchanged states stay silent.
- Labels every price as **SPY**: continuation trigger, invalidation, confirmed-close entry reference and 2R target. These are signal references, not executable SPX option prices or guaranteed fills.
- Keeps `!status` cached and restricted to the configured private channel; `!help` is available. No old `!bb`, dashboard or research commands remain.

See [exact initial rules](docs/strategy.md). Thresholds are design conventions requiring independent validation, not optimized or proven rules.

## Setup

Python **3.12+** is required by the official Theta SDK.

```bash
python -m pip install -r requirements.txt
# NEW installations only; never overwrite an existing .env:
cp .env.example .env
python main.py
```

Edit the existing `.env` privately on upgrades. Set `DISCORD_TOKEN`, `THETADATA_API_KEY`, `SPX_CHANNEL_ID`, and explicitly enable `COLLECTION_ENABLED=true` and `PUBLICATION_ENABLED=true` when ready. Both switches default off. `SPX_POLL_SECONDS` accepts 30–60 seconds. Old `SPX_CONTEXT_*` aliases are **no longer read**; migrate their values manually to the current names.

Enable Discord Message Content Intent and verify the destination's private access and the applicable data licence. Collection off means no provider requests. Publication off prevents alerts but does not stop explicitly enabled collection or vendor billing.

**Feed requirement:** SPY stock OHLCV, not index-only access. The adapter explicitly requests Theta's `nqb` (Nasdaq Basic) venue for current and historical minutes. VWAP and volume are feed-specific, not claimed consolidated-market measures. Confirm account entitlement and live/history comparability before enabling. No subscription is changed by this code.

## Evidence and safeguards

New state lives under `.thetadata-data/spy-setups/`; old recordings are preserved but never migrated into SPY setups. First-seen bars and volume are immutable. Missing, stale or malformed data fails closed. Five preceding sessions supply matching-minute volume baselines; at least three positive samples are required. Starting late can mean no qualifying setup that day; history is not allowed to invent earlier signals.

Lifecycle identity, not direction alone, drives durable deduplication. An event expires for publication 90 seconds after its transition close; later candles do not extend it. No post-close alerts. Suppressed events are not replayed when publication is enabled. Ambiguous Discord sends are not automatically retried. Atomic evidence writes and a process lock protect collection/delivery; never delete `.service.lock` to bypass a running owner.

Optional `THETA_AUDIT_START_DATE` / `THETA_AUDIT_DAYS` control local request metadata only. Preserve the original audit date. Logs and provider evidence remain private and ignored. Establish a private, licence-compliant backup separately.

## Validate and deploy

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
python -m compileall -q bot core spy_setup tests
python -c "import main, bot.core, spy_setup.cog"
git diff --check
```

Offline tests cover synthetic rules and safety contracts, not profitability, SPX option P&L, live entitlement, feed correctness or Discord permissions. This strategy requires fresh independent evaluation; old recorded sessions are not part of its operation.

For the existing Pi: review and merge changes to remote `master`, stop the old service and verify it has exited, then pull in the existing clone, preserve `.env`, install dependencies and review the current configuration names. Use `make restart`, `make status`, `make tail`. Process status is not data-health proof; check cached Discord status and logs. No remote deployment or paid request is implied by local validation.

## Repository boundary

`bot/core.py` registers the single `SpySetupCog`; `spy_setup/` owns rules, rendering and collection; `core/` owns OHLCV evidence, persistence, locking, calendar and provider plumbing. Retired indicator strategies, research tools, dashboards and their tests have been removed, not hidden behind modes. Stored `data/`, logs and credentials are not dead code and remain untouched.
