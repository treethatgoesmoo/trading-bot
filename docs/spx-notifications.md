# Retired unified SPX notifications — historical deployment and behavior

**Superseded by [BB notifications](bb-notifications.md).** The owner requested removal of this SPX structure/action indicator. Local active assembly now emits completed 1m/5m/15m BB events with RSI/MACD, without a mode selector, flow/proxy collection or legacy streams. This document is retained for historical ledger/rule interpretation only; do not apply its deployment settings or treat its commands as current. Original context evidence and frozen ORB history are preserved. No new deployment is confirmed.

## Status and migration

The owner requested replacement of **all existing automatic notifications** with one SPX structure/setup/momentum/confirmation/invalidation stream. Implementation merged through PR #39; Pi deployment is not verified. **The owner subsequently required removal of legacy notifications and the mode selector; that correction is pending.** See [session-handoff.md](session-handoff.md). The mode/rollback instructions below describe the currently merged code only. Do not treat the existing Pi process as running this version.

The owner already cloned the repository on the Pi: deployment is **pull `master`, then `make restart`** from its repository directory. Changes must first reach remote `master`. No systemd/Docker procedure is required.

1. Verify the intended Discord channel is private to the owner and its use complies with the data license. The bot checks the configured channel ID for commands, not its membership/permissions. Do not post licensed raw rows or expose derived licensed context in a public/shared channel.
2. Preserve the Pi's existing ignored `.env`, including `DISCORD_TOKEN` and the valid `THETADATA_API_KEY`. Do not replace it with the example file or share secrets.
3. Add/update the following settings before restarting:

   ```env
   NOTIFICATION_MODE=spx
   SPX_CONTEXT_CHANNEL_ID=123456789012345678
   THETA_AUDIT_START_DATE=YYYY-MM-DD
   THETA_AUDIT_DAYS=30
   SPX_CONTEXT_FLOW_ENABLED=1
   SPX_CONTEXT_PROXY_ENABLED=1
   ```

   Replace the example numeric ID with the actual private channel. Choose a fixed **ET calendar date**, preferably before the first study session opens; do not reset it on restart. Align the review with the real subscription renewal date.
4. Once the changes are on remote `master`, pull `master` on the Pi. Update dependencies if required using the existing Python environment; the Theta SDK requires Python 3.12+.
5. Run `make restart`, then `make status` and `make tail`.

**Fail-closed migration:** SPX mode defaults on, but its channel defaults to `0`. With no channel, the bot can collect locally but cannot publish. It never substitutes `ALERT_CHANNEL_ID`. With no Theta key, the context collector is not installed. Neither case re-enables legacy alerts. An invalid notification mode refuses to start the trading stream.

### Verify readiness, not just startup

- Logs should identify `Notification mode=spx` and `Unified SPX stream installed`. Investigate missing-key/channel warnings.
- After 09:31 ET on a trading day, confirm `.thetadata-data/context/context-YYYY-MM-DD.json` advances with successful collection and received bars.
- During the active audit window, confirm `usage-YYYY-MM-DD.jsonl`, `context-YYYY-MM-DD.json`, and `orb-YYYY-MM-DD.json` appear under `.thetadata-data/audit/`.
- From 09:45 ET, verify an actual six-field message (Action first) reaches the private channel. The ledger should show `delivery=sent` with a delivery timestamp—not only a request-start heartbeat or pending intent.
- Exercise `!spx` and `!audit` there. Check fresh timestamps against a trusted SPX screen; inspect missing inputs rather than assuming “available” means a complete real-time feed.
- Confirm old forecasts, Bollinger/volume pings, swing suggestions, full-shadow trade alerts, economic briefings, restart notices, and retune posts no longer arrive automatically.
- Repeat checks over the first two sessions, including a post-close report. A healthy process is not proof of healthy data or Discord delivery.

## Experimental Action checklist

`spx-action-retest-v1` is a frozen, **unvalidated price-only paper checklist**, not a proven strategy or executable option recommendation. `zero_dte/spx_actions.py` is the source of truth. Action appears first, followed by the existing five context sections.

| Action | Exact condition, in precedence order |
|---|---|
| **NO TRADE** | Stale/missing/incomplete SPX session evidence; no reliable current range structure. |
| **WAIT FOR REVERSAL CONFIRMATION** | A failed upside/downside attempt returns inside the opening range. This does not establish an opposite entry. |
| **NO TRADE** | Inside range without a fresh failed attempt. |
| **WAIT FOR CONFIRMATION** | First breakout or renewed/reclaimed boundary break; follow-through not yet established. |
| **WAIT FOR RETEST — DON'T CHASE** | Holding outside the range with direction-aligned five-minute momentum that is steady or strengthening, but no current boundary retest. This is a procedural wait, not an extension/distance measurement. |
| **CONSIDER CALLS / CONSIDER PUTS — experimental** | Current completed minute revisits the relevant range boundary and closes outside, previous close was already outside on that side, and completed five-minute momentum is aligned and steady/strengthening. UP maps to calls; DOWN maps to puts. |
| **NO TRADE** | Holding/retest with fading, mixed, opposed or unavailable momentum; any unsupported combination. |

Candidates require fresh complete SPX evidence. Prior-session levels, Yahoo SPY and experimental flow are **contextual only**, even when they oppose a candidate: none gates this version. The candidate text requires manual checking of the actual 0DTE contract, fresh bid/ask, spread, time to expiry and acceptable premium loss in Robinhood. It selects no strike, premium, fill, position size or option stop. No position knowledge means no hold/sell/exit instructions.

Every assessment stores `action.version`, `state`, `candidate_direction`, `reason` and `source_roles`. SPX is `rule_input` or a failed-data `quality_gate`; other sources are `contextual_only` or `not_used`. Existing `consumed_request_ids.spx` carries the request linkage. Changing these rules requires a new version and separate evidence, not tuning this month's sample. The frozen first-close ORB experiment is unchanged.

Candidate withdrawal bypasses cooldown. Action is included in deduplication while the invalidation signature remains last. Expired cached views explicitly show NO TRADE; fresh `!spx`/`!status` retain the action and enter the existing delivery ledger without new requests.

`!audit` adds a sent-only, version-specific action section. It counts all six states, but only call/put candidates receive validated existing 15/30-minute signed SPX movement. Wait/no-trade messages receive no invented zero return, correctness, or avoided-loss credit. Missing/invalid/unknown outcomes remain visible. Old rows without Action are unclassified, not retrofitted; unknown versions or inconsistent metadata are excluded from action scores. No option returns or demonstrated edge are inferred. This version has no separate late-session entry cutoff; close-crossing horizons are `session_ended`, not scored as losses or zero returns. Near-expiry suitability remains a manual check.

## What the five context sections mean

| Section | Evidence and interpretation |
|---|---|
| **Structure** | Position relative to the complete 09:30–09:44 opening range; observed session extremes; prior-session high/low/close only with complete immediately prior-session coverage. Levels are references, not a daily-bias prediction. |
| **Setup** | Inside range, first close outside a boundary, holding outside, observed retest, failed attempt, or reclaim. Session gaps make the sequence indeterminate. The experimental Action checklist may flag an aligned retest candidate; this does **not** restore the retired strategy or change the frozen ORB rule. |
| **Momentum** | Two completed, non-overlapping, session-aligned five-minute candles: higher/lower highs and lows plus changing net movement. Strengthening/fading describes observed behavior, not a probability or reversal forecast. |
| **Confirmation** | Optional indicative Yahoo SPY VWAP/volume and experimental sampled SPXW flow. Missing/stale inputs are unavailable—not opposing votes. Neither changes the price-derived direction. |
| **Invalidation** | The completed-close condition that contradicts the current structure interpretation. It is a scenario boundary, **not an option stop or contract recommendation**. |

SPX direction means current opening-range location, not “the next 15 minutes will rise/fall.” No confidence percentage, automated order, option selection, or promised turning point is added.

### Data and timing

- One SPX index collector requests current-session completed one-minute bars from the earliest missing minute. First-seen valid bars and receipt times persist.
- The immediately previous trading session is requested separately and cached after complete coverage; failed/incomplete attempts retry at most every 15 minutes. Holidays and early closes come from the local market calendar.
- SPX freshness is measured from completed-bar end, not merely request completion. Missing session minutes prevent asserting a trustworthy breakout sequence.
- Optional Yahoo SPY data is **indicative, not guaranteed real-time or executable**. VWAP uses cumulative regular-session typical-price volume. Relative volume compares the last completed five-minute block with the mean of the preceding three five-minute blocks. It needs contiguous valid minutes and sufficient history; SPY volume is not SPX volume.
- Optional SPXW flow uses the quiet cache for the session, only while the explicit audit window is active. It requests narrow-strike trade/preceding-NBBO history, not chains, Greeks, option exits, or index data. The cache and latest trade must be fresh, and classification coverage sufficient. Aggressor inference cannot reveal dealer inventory, opening/closing intent, or complete multi-leg activity.

### Posting policy and controls

Automatic observations begin no earlier than **09:45 ET**. Default polling is 60 seconds; routine state changes have a 120-second cooldown; unchanged context has a 30-minute heartbeat. Breakout/failure/reclaim, invalidation-state changes, and stale-data transitions can bypass the routine cooldown. Moving numbers alone do not force another post.

```env
# Optional controls; defaults shown
SPX_CONTEXT_POLL_SECONDS=60
SPX_CONTEXT_COOLDOWN_SECONDS=120
SPX_CONTEXT_HEARTBEAT_MINUTES=30
```

Collection starts after the first minute completes and allows five minutes after the regular/early close to finish the final bar. That completion allowance is not an after-hours notification stream.

Provider calls are synchronous work offloaded to threads with single-flight collection. Three current-session failures pause polling for five minutes. A permanently hung SDK/Yahoo worker can still stall the collector; cancellation is not a safe hard timeout. Watch heartbeats and latency instead of assuming a loop guarantees uptime.

## Commands and delivery grading

- `!spx` and `!status` use cached current evidence without provider requests. A fresh on-demand display is added to the same delivery/grading ledger. Expired evidence yields unavailable context rather than a stale actionable read.
- `!audit` attaches a read-only summary in the configured private channel; it makes no paid endpoint calls and includes no raw market rows.
- `!stats` explains that legacy forecast/swing statistics are historical and points to `!audit`. Legacy dashboards/research and manual administrative commands are not evidence for the new stream.

Publication intent is saved before Discord sending. Successful acknowledgement sets the grading availability time. An interrupted pending send becomes unknown on restart; exactly-once delivery cannot be guaranteed across a network/process failure. Suppressed, failed, unknown, and sent assessments remain distinguishable.

Sent observations receive 15- and 30-minute forward **SPX movement** from the next minute open strictly after delivery availability. Missing intermediate bars, stale delivery, and horizons crossing the session close are explicit. Finalized missing outcomes do not turn into winners after recovery backfills. Neutral descriptions receive no invented directional correctness score.

These observations can overlap and are correlated. Signed SPX points are **not independent trade returns, option P&L, or proof that the five-section description predicts price**.

## Subscription linkage and expiry

Read [the audit protocol](research/theta-subscription-audit.md) for reporting and renewal decisions.

- `spx_context` labels index requests; `spx_context_flow` labels the optional options sampler.
- Each assessment records source incorporation and request provenance; delivered assessments are separately countable. Cached data can support many messages without becoming many consumed requests.
- Constituent flow-request IDs are separate from the latest poll ID. An empty refresh may be operational evidence, but is not returned-trade consumption.
- The same SPX data feeds the **unchanged frozen ORB experiment**. No second current-session audit collector is installed in replacement mode.
- Live context records continue under `.thetadata-data/context/` after the audit ends. Options sampling, request telemetry, and mirrors under `.thetadata-data/audit/` stop with the bounded window. **Index requests and vendor billing continue.**

`SPX_CONTEXT_FLOW_ENABLED=0` disables replacement-mode options sampling; `SPX_CONTEXT_PROXY_ENABLED=0` disables Yahoo proxy requests. `THETA_AUDIT_FLOW_ENABLED` controls only the old legacy audit sampler and has **no effect** on replacement-mode flow.

## Local validation

188 offline regression tests passed (178 pre-Action baseline plus 10 Action regressions), including replacement assembly, stale/gapped data,
causal delivery grading, neutral-context confirmation reversals, empty-refresh provenance,
restart deduplication, private-channel commands, and audit expiry. Action tests additionally cover symmetric completed-bar candidates, all wait/no-trade branches, stale cache suppression, immediate candidate withdrawal, contextual-only source roles, legacy/tampered metadata exclusion, and candidate-specific causal/missing/pending outcomes. Compilation, imports,
and `git diff --check` also passed. Tests used Python 3.11.3 with mocked providers; the
live Theta SDK still requires Python 3.12+. No paid calls, Discord delivery, Pi service,
or profitability were validated by these checks.

## Legacy compatibility and non-goals

`NOTIFICATION_MODE=legacy` is an explicit rollback to the retained feature toggles. In default SPX mode, `ZERO_DTE_ENABLED`, `SWING_ENABLED`, and `SPX_FLOW_ENABLED` cannot revive the old trading streams. Automatic legacy tuning is also suppressed; quiet tracked-history auto-commit remains.

Existing history is preserved, not converted into SPX context results. New-mode collection does not continue legacy cogs' outstanding paper-signal lifecycle. No files in committed `data/` are hand-edited or removed. Research, private backup/retention, exact-contract option validation, and multi-regime historical strategy replay remain separate work. The context migration is a UI/data-use change; the added Action checklist is a new experimental hypothesis, not adoption of a proven trading edge. No multi-regime replay or executable option backtest validates it yet.
