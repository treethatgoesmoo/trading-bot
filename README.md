# SPY setups → SPX 0DTE

A private Discord service watching **SPY price structure, VWAP and relative volume** for opening-range breakout/retest setups. Execution is manual in **SPX 0DTE options**. No broker orders, contract selection, premium targets, confidence score or validated trading edge.

## What it does

- Builds SPY's first 15-minute regular-session range from completed 1-minute OHLCV candles.
- Requires a breakout aligned with session VWAP and at least 1.2× matching-minute relative volume, then a later retest and a later continuation close.
- Publishes distinct lifecycle transitions: forming (breakout/retest), triggered, invalidated, expired or target reached. **Forming is not an entry.** Idle/unchanged states stay silent.
- Labels every price as **SPY**: continuation trigger, invalidation, confirmed-close entry reference and 2R target. These are signal references, not executable SPX option prices or guaranteed fills.
- Keeps `!status` cached and restricted to the configured private channel; `!help` is available.
- `!preview` posts five clearly labeled fictional examples in that private channel (forming, triggered and three alternative endings). No market requests, live-state writes or enabled collection/publication required; limited to once per channel per 30 seconds. No old `!bb`, dashboard or research commands remain; new automatic informational BB notifications are described below.

### Informational range and Bollinger Band notifications

Alongside the stricter setup signals, the same collection/publication opt-ins enable independent **informational SPY notifications — not entry signals**:

- First 15-minute opening-range high/low at **6:45 a.m. Pacific**.
- A completed **1m close** crossing above that high or below that low.
- A completed **5m or 10m close** crossing outside its upper/lower Bollinger Band: **20 periods, 2 population standard deviations**, close-based SMA. Each close is compared with its own rolling bands. Touches, intrabar wicks and returning inside do not notify; staying outside does not repeat. A new outside crossing can notify again after returning inside.
- Five- and ten-minute candles align to 09:30 ET and require every constituent completed minute. Bands use **today's regular session only**, not overnight or prior-session closes. Both previous/current bands are required (21 candles), so the first possible BB crossing alerts are **8:15 a.m. Pacific (5m)** and **10:00 a.m. Pacific (10m)**. Ten-minute BBs cannot notify on a 3.5-hour early-close session because warm-up ends at market close, when sends are blocked. **30m BBs are not enabled**: 21 candles require 630 minutes, exceeding a regular 390-minute session. No prior-session history or extra requests are added. Charts with extended-hours/prior-session warm-up may differ.

No VWAP, relative-volume, retest or noon entry cutoff is required for these informational events. They operate during regular market hours, with no post-close sends. Missing/invalid bars fail closed; an event must still be within **90 seconds of its candle close**. Late starts do not replay old range announcements or crossings. Notifications are not guaranteed during feed/network outages.

Separate `spy-informational-v2` journals live under `.thetadata-data/spy-setups/informational-v2/YYYY-MM-DD/`. The v2 timeframe change does not migrate, relabel or delete v1 journals; current informational delivery counts read v2 only. Deploy between sessions: a mid-session version switch gives even unchanged range events new IDs and could notify a still-fresh range event again. Each event reserves a durable intent before sending. **Any existing intent prevents retry**, including ambiguous, rejected and suppressed sends. A crash between reservation and sending can lose a notification rather than duplicate it. Preserve these journals. This does not change setup rule versions or rewrite old setup ledgers. `!status`, `!day` and automatic daily reports show separate informational delivery counts; zero setup entries does not imply zero informational notifications.

### Projected daily SPY range: `!range`

Private-channel-only, on-demand **historical estimate of today's SPY low–high**, not an entry signal or guaranteed support/resistance. No automatic range alerts. Available after the first completed opening minute (6:31 a.m. Pacific); before open, holidays, incomplete or stale data return unavailable.

- Uses the **20 preceding trading sessions**, including early closes. For each session, measures percentage moves from its open to its low and high. Separately takes the linearly interpolated **80th percentile** of each excursion and applies those distances to today's open. This is **not an 80% full-session coverage guarantee**; accuracy is unvalidated. No options-implied volatility or SPX price conversion is used.
- Shows the estimated bounds, accepted opening anchor, actual low–high so far, latest completed close, Pacific timestamp, sample dates and observed breaches. After close, requires a complete session and labels the observed full-day range. Prices can exceed the estimate.
- Keeps the historical sample and first accepted opening anchor in memory for that session so subsequent requests do not chase price or revised opening candles. **Restarting clears this cache**; revised provider history/open prices can change a newly calculated estimate. This is not a persisted forecast or an accuracy-tracking system. Feed differences, splits, news and volatility-regime changes can distort historical estimates.
- Requires the existing **collection opt-in and credentials**. Publication opt-in does not gate this explicit command reply. First use can make up to **22 sequential provider requests** (20 prior sessions, today, and at most one current-data refresh after slow warm-up). Cached use makes at most two. Provider usage may be billable; no background collection is added. Failed warm-up/current requests may require history to be fetched again on the next invocation.
- One range request at a time; five-minute cooldown on initial/uncached attempts, one minute on cached attempts. Cache lasts until restart or the next session. An isolated client and cancellation-safe worker use no live setup or delivery journals. No automatic retries of failed requests or sends. Historical timestamps reflect actual retrieval, not simulated live observation.

### Live daily records and command guide

- `!cmd` lists the currently registered, enabled commands with usage/help (including `!help`); it uses the configured command prefix, not a retired static list.
- `!day` reads today's saved **live** evidence; `!day YYYY-MM-DD` reads a prior trading date. Both commands are private-channel-only. No provider requests, replay, evidence writes or strategy reconstruction. Missing, corrupt or incompatible-version evidence returns unavailable rather than zero signals.
- Reports deduplicate actual observed lifecycles by setup ID: formed, triggered entries, invalidated/expired before observed entry, and target/invalidation/expiry/unknown outcomes of observed entries. SPY references are not fills, trades or SPX option P&L. Unobserved transitions are never reconstructed; missing trigger evidence is not invented.
- Reports include missing completed minutes, late receipts, history readiness, last poll and delivery counts. Even complete candles do not prove complete live observation coverage; reports explicitly label that limitation. Pending alert delivery is reported as unknown.
- With collection and publication enabled, the monitor attempts one current-session report between **6 and 30 minutes after calendar close** (after the collector's five-minute final-bar grace period), including early closes. It reads the saved ledger only and does not fetch final missing bars. Offline/missed windows have no automatic next-day catch-up; use `!day` manually.
- A separate `daily-delivery-YYYY-MM-DD.json` reserves the automatic send durably before Discord is contacted. Existing intents (including uncertain/interrupted sends) are never retried automatically. A crash before sending can therefore lose a report rather than duplicate it. Manual `!day` remains available and does not alter that journal. Preserve journals with evidence backups.

### Historical Discord replay

- `!replay` mocks the previous trading date (even after today's close); `!replay YYYY-MM-DD` selects a prior trading session within the last 30 calendar days. Today, future dates and non-trading dates are rejected.
- Explicitly requests up to **six historical sessions** (target plus five baseline sessions) using a separate Theta client. Requires credentials and stock entitlement; works independently of collection/publication flags and may incur provider usage. No automatic retries.
- Posts chronological production-formatted embeds labeled **REPLAY — NOT A LIVE SIGNAL**, with the session date and assumed timely-arrival disclaimer on every embed. Up to ten event embeds plus progress/summary; zero triggered entries is reported explicitly. These are a sequence, not fictional alternative endings.
- Assumes history was available at 09:29 ET and candles arrived five seconds after close. Historical prices may have been revised; replay does not establish actual alerts, executable fills, option returns or profitability. Incomplete sessions abort rather than imply no signals.
- Only the configured private channel; once per channel per five minutes and one replay at a time. Invalid dates, wrong-channel requests, missing credentials and shutdown rejections do not consume or reset cooldown. Cooldown and busy attempts receive explicit Discord replies. The cooldown starts when a valid replay is accepted, including if its provider request later fails. No live ledger, delivery intent, first-seen receipt or recorded-bar writes. The provider's existing local request-metadata auditing still applies. Review private-channel access and data licensing before use.

Replay logs show acceptance/rejection, cooldown or busy state, each historical request and elapsed time, computed event/entry counts, and Discord send progress. Failures log exception classes and stack locations, never exception messages or provider payloads. Routine Discord gateway/client INFO chatter is suppressed; warnings/errors remain. File log timestamps retain the host's configured timezone.

Alerts put bullish/bearish direction first, with green/red crossing colors and the completed-candle price change on informational crossings. Setup context shows the latest close's VWAP position and relative volume when available; setup direction is not a separate momentum forecast. Price fields stay labeled SPY without repeated execution disclaimers.

Operational logs include poll health, freshness, bar/history counts, failures, elapsed time and delivery outcomes with event correlation IDs. Poll summaries appear on health changes and at least every five minutes while polling; unchanged polls and duplicate informational events are DEBUG. Informational reservations record suppression/failure reasons and event age. Logs exclude candle payloads, credentials and exception messages; preserve journals as the durable delivery evidence.

Discord alerts, previews, replay and status timestamps use Pacific time (PST/PDT automatically). Strategy schedules remain Eastern; stored timestamps remain UTC.

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

Enable Discord Message Content Intent and verify the destination's private access and the applicable data licence. Collection off stops automatic provider requests; an explicit `!replay` still requests historical data. Publication off prevents alerts but does not stop explicitly enabled collection or vendor billing.

**Feed requirement:** SPY stock OHLCV, not index-only access. The adapter explicitly requests Theta's `nqb` (Nasdaq Basic) venue for current and historical minutes. VWAP and volume are feed-specific, not claimed consolidated-market measures. Confirm account entitlement and live/history comparability before enabling. No subscription is changed by this code.

## Evidence and safeguards

New state lives under `.thetadata-data/spy-setups/`; old recordings are preserved but never migrated into SPY setups. First-seen bars and volume are immutable. Missing, stale or malformed data fails closed. Five preceding sessions supply matching-minute volume baselines; at least three positive samples are required. Starting late can mean no qualifying setup that day; history is not allowed to invent earlier signals.

Lifecycle identity, not direction alone, drives durable deduplication. An event expires for publication 90 seconds after its transition close; later candles do not extend it. No post-close actionable alerts; the separate daily evidence summary is allowed in its bounded post-close window. Suppressed events are not replayed when publication is enabled. Ambiguous Discord sends are not automatically retried. Atomic evidence writes and a process lock protect collection/delivery; never delete `.service.lock` to bypass a running owner.

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
