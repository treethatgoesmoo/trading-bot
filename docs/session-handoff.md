# Session handoff — BB replacement implemented locally

## Latest session — supersedes earlier implementation instructions below

The owner found the SPX signals unhelpful, proposed 1m/5m/15m BB contacts with RSI/MACD, requested research, then instructed: **"make those changes. remove this spx indicator"**. Implemented as replacement of the indicator/notifications, retaining SPX as the market. Do not restore the old Action/context system or legacy selector.

- Active assembly: only `SharedCog` + `BBSignalCog`. New files `zero_dte/bb_signals.py`, `bb_signal_embeds.py`, `bb_signal_cog.py`; event-only posts, cached `!bb`/`!status`, current BB `!audit` counts. No paid AI integration.
- BB(20,2), RSI(14), MACD(12,26,9), completed 1m/5m/15m candles, two complete preceding sessions, receipt/freshness gates, restart-safe deduplication, durable sent/failed/unknown/suppressed ledgers. No old action advice or heartbeat. Latest timeframe events remain eligible for 90 seconds after their own close so skipped polls can recover; IDs prevent repeats.
- Old stream registrations and mode/feature selector removed, along with shared forecast/economic/tuning/startup notifications/commands. Historical cogs/engines remain unregistered for historical tests/readers. No options-flow or SPY proxy requests. Frozen ORB evidence preserved with validated same-day context/mirror migration; contradictory migration sources fail closed.
- Same private channel/poll env keys remain for Pi compatibility. `NOTIFICATION_MODE` and old notification flags have no effect. Preserve `.env`, credentials, original audit date and committed history. No vendor/subscription change or remote/Pi action performed.
- Licensed BB evidence: `.thetadata-data/bb-events/`; audit mirrors separate from retired context. Telemetry purpose `bb_events`. Old audit reports do not grade new BB observations; BB outcomes remain **not evaluated**. No held-out event study or option-profitability claim.
- Validation: **252 offline tests passed plus 17 subtests**, compile/import smoke and whitespace checks. No live SDK/Discord/Pi test. Current sources: [BB contract](bb-notifications.md), README, [research memo](research/bb-multitimeframe-and-ai-tools.md).
- Next: review diff (pre-existing documentation edits were preserved), commit/PR/merge as authorized, then Pi pull/restart and private market-hours checks. Resolve any conflicting old context/ORB migration evidence explicitly—do not delete evidence. Separately implement the held-out event study before claiming trading usefulness.

## Historical handoff before the BB request

Everything below is retained for provenance. Its keep-SPX-actions and removal-pending instructions are superseded above.

## Start here next time

**The owner's latest instruction supersedes the rollback design:** remove the old notification systems, not merely disable them behind `NOTIFICATION_MODE`. The assistant acknowledged that retaining `legacy` mode was a misinterpretation. **Removal has NOT been implemented.** Do not ask whether the owner wants a legacy toggle again.

1. Start the removal work from current remote `master`, preserving local documentation changes.
2. Remove notification-mode selection, legacy notification registration/cogs/commands and obsolete configuration. Trace dependencies before deleting files; preserve shared infrastructure and genuinely needed research readers.
3. Keep the unified SPX stream, its experimental Action checklist, quiet data collection, frozen ORB comparison, subscription telemetry, reporting and grading.
4. Test and document the removal, then prepare a follow-up PR. Do not mistake PR #39 for completion of this correction.

## Verified repository status

- [PR #39](https://github.com/treethatgoesmoo/trading-bot/pull/39), **Replace trading alerts with audited SPX context and experimental actions**, is **MERGED**. GitHub reports merge time `2026-09-08T00:54:05Z`; remote `master` contains squash commit `5adecdb`.
- Original implementation branch: `feat/spx-context-actions-audit`, implementation commit `73f695f`. Its earlier “not pushed/merged” status is superseded by the GitHub check above.
- [PR #38](https://github.com/treethatgoesmoo/trading-bot/pull/38), the earlier documentation-only roadmap, remains OPEN at this check. #39 includes that roadmap; review whether #38 is redundant before closing it. It was not modified or closed in this session.
- This documentation handoff was started on `docs/spx-session-handoff` from remote `master`. Do not infer that these handoff edits are committed/pushed.
- **Pi deployment and audit activation are unknown.** The user asked about environment values and editing with vi, but did not confirm a pull, restart, selected channel/start date, successful notification, or advancing audit files. A GitHub merge is not deployment evidence.

## Owner requirements — do not re-ask

- Trades **SPX options, 0DTE**, manually through **Robinhood**. Owner reports no usable broker API. Bot never submits orders.
- Pays about **$130/month** for ThetaData **Options Standard + Index Standard**. Wants the already-paid month used to measure subscription demand, quality and actual usefulness before choosing downgrade/replacement.
- Wants one understandable, actionable SPX stream, replacing all old notifications—not another stream and not a legacy-mode selector.
- Approved Action labels: CONSIDER CALLS/PUTS — experimental; WAIT FOR CONFIRMATION; WAIT FOR RETEST — DON'T CHASE; WAIT FOR REVERSAL CONFIRMATION; NO TRADE.
- Wants a strategy that actually works. No profitable strategy has been established; implementation correctness and underlying direction are not executable option profitability.
- Pi repository is **already cloned**. Deployment is **pull `master`, then `make restart`** in the repository. Preserve the Pi's ignored `.env`; do not ask for credentials or re-ask the deployment method.

## Work completed in #39

### Unified context and delivery

`zero_dte/spx_context.py`, `spx_context_embeds.py`, `spx_context_cog.py`, `spx_proxy.py`:

- Action first, then Structure, Setup, Momentum, Confirmation, Invalidation.
- Complete 09:30–09:44 ET opening range; current session extremes and complete immediately prior-session high/low/close.
- Completed-minute breakout/hold/retest/failure/reclaim descriptions and two completed, nonoverlapping session-aligned five-minute momentum candles. Missing/stale/gapped evidence is unavailable, not a directional vote.
- Optional Yahoo **SPY indicative proxy**, not SPX volume or guaranteed real-time data: session VWAP and latest completed five-minute volume divided by the mean of the preceding three blocks.
- Experimental sampled SPXW flow provides context only; neither flow nor SPY changes price-derived direction.
- First automatic observations after 09:45 ET; meaningful-change posting, default 120-second cooldown and 30-minute heartbeat. Important state changes and candidate withdrawal bypass routine cooldown.
- `!spx`/`!status` use cached evidence without new requests. Fresh on-demand observations are logged and graded; expired views show NO TRADE/unavailable. `!audit` generates a private offline summary.
- Delivery intent is saved before sending; grading starts from Discord acknowledgement. Restart ambiguity stays unknown, not presumed delivered. Exactly-once delivery is not guaranteed.
- One SPX collector feeds context and the frozen ORB audit. Completed-minute cutoff, earliest-gap recovery, ET/UTC and early-close handling avoid incomplete bars and duplicate current-session collectors. Unchanged archives are not rewritten at reconciliation.

### Frozen experimental Action checklist

`zero_dte/spx_actions.py`: **`spx-action-retest-v1`**, a price-only prospective paper hypothesis.

- First/reclaimed break: WAIT FOR CONFIRMATION.
- Holding outside with aligned steady/strengthening momentum: WAIT FOR RETEST.
- Current boundary retest closes outside, prior close already outside, aligned steady/strengthening five-minute momentum: CONSIDER CALLS or PUTS — experimental.
- Failed attempt back inside: WAIT FOR REVERSAL CONFIRMATION, not automatic opposite entry.
- Inside range, stale/incomplete inputs or nonqualifying momentum: NO TRADE.
- Source roles distinguish SPX rule inputs/quality gates from prior levels, SPY and flow used only as context. Opposed flow currently does NOT veto a price-only candidate; do not claim it drove the action.
- Messages require manual contract, bid/ask, spread, time-to-expiry and premium-risk checks. No contract selection, fills, position sizing or knowledge of owner holdings. No separate late-session action cutoff yet.

### Subscription and outcome audit

`core/theta_usage.py`, `core/thetadata_client.py`, `zero_dte/flow_cog.py`, `audit_cog.py`, `orb_audit.py`, `core/context_audit_report.py`, `report_theta_audit.py`:

- Explicit fixed ET start and 30 **calendar** days; restarts do not reset the window.
- Paired SDK start/finish metadata: purpose/product/endpoint, request ID, duration, success/error class and row count. No credentials, raw payloads, arguments or exception messages in this telemetry. This is not a repository-wide secret-log guarantee.
- Purpose labels distinguish operational SPX/context flow, legacy flow, audit, research collector and diagnostics. Request counts are not vendor billing units; raw overlapping rows are not unique observations.
- Distinguish requests fetched, sources incorporated in assessments, and observations actually delivered. Retained-trade constituent IDs are authoritative; empty refreshes do not receive constituent-data credit.
- Flow requires fresh poll and latest-trade times plus classification sufficiency. Freshness does not establish complete market coverage.
- Delivered context/action candidates receive descriptive 15/30-minute SPX movement with causal next-minute availability. Missing bars, stale delivery, session-close censorship and historical uncertainty remain visible. Wait/no-trade actions are not scored as trades; older rows are not retrofitted with actions.
- Separate frozen **`spx-orb-15m-first-break-v1`**: first eligible completed close outside the full opening range, at most one candidate/session; next-minute open strictly after receipt, opposite-range stop, 1R target, at most 30 minutes, conservative ambiguity/gap handling. Prospective receipt and bar close must precede 11:00 ET, within 90 seconds of close. Late startup is missed, not a later substitute entry.
- ORB comparison uses identical evaluable-flow candidate sessions for baseline versus flow-filtered policy; filtered candidates get zero policy R, missing evidence stays separate. This experiment is not replaced by the new retest Action rule.

### Validation completed

**188 offline tests passed**, plus compilation, import smoke tests and whitespace checks. Synthetic evidence/mocked providers on Python 3.11.3; live Theta SDK requires Python 3.12+.

Regressions cover replacement assembly, stale/gapped data, early closes and UTC/ET scheduling, request lineage/empty refreshes, delivery timing, restart uncertainty, no-backfill outcome rewriting, confirmation reversals, action symmetry/withdrawal, cached commands, report denominators and expiry.

No paid endpoint, live Discord/Pi, subscription savings or profitability validation was performed by these checks. There is no honest replay harness for the retired intraday ensemble/break-and-retest; a profitable new entry rule has not been demonstrated.

## Next implementation — removal acceptance checklist

- [ ] Remove `NOTIFICATION_MODE` from `config.py`, `.env.example`, assembly and documentation. No `legacy` fallback even when old `.env` flags remain set or Theta/channel configuration is missing.
- [ ] Remove old forecast, BB, volume, swing and full experimental-shadow notification startup paths, cogs, commands and their obsolete knobs. Inventory imports/callers first, including manual legacy dashboard/stats surfaces in `bot/shared_cog.py`.
- [ ] Refactor `SPXFlowCog` rather than blindly delete `zero_dte/flow_cog.py`: its quiet cache still supplies unified confirmation/audit data. Remove full-watch/legacy notification branches while preserving needed sampling, freshness, circuit breaker and lineage behavior.
- [ ] Remove standalone legacy `AuditCog` registration but preserve/refactor shared `capture_flow`, calendar/collection and ORB helpers used by the unified collector. One current-session collector only.
- [ ] Preserve needed operational/admin helpers and quiet history housekeeping; do not accidentally remove bot startup, error handling or all shared utilities with an old cog.
- [ ] Preserve committed `data/` history and backward-compatible research readers where still needed. Do not delete history just because its producing notification system is removed. Prune dependencies only after checking shared/research consumers.
- [ ] Replace tests that intentionally exercise legacy assembly with tests proving legacy streams/commands are absent. Old environment flags must not resurrect them. Preserve context/action/source lineage/grading/expiry tests.
- [ ] Update README, CLAUDE, `.env.example`, state and migration docs to distinguish removed functionality from historical evidence. Run full tests, compile/import smoke and `git diff --check`; create a follow-up PR when ready.

This is code cleanup to fulfill owner intent, not permission to retune the frozen Action or ORB rules or auto-deploy.

## Deployment and first-session checks after cleanup

Current merged implementation still requires `NOTIFICATION_MODE=spx`; this setting becomes obsolete once removal ships. Other required/current settings:

```env
SPX_CONTEXT_CHANNEL_ID=<actual-private-Discord-channel-number>
THETA_AUDIT_START_DATE=<actual-fixed-ET-YYYY-MM-DD-start>
THETA_AUDIT_DAYS=30
SPX_CONTEXT_FLOW_ENABLED=1
SPX_CONTEXT_PROXY_ENABLED=1
```

Neither channel number nor start date has been supplied/confirmed. The earlier `2026-09-08` date was an **example**, not an activation decision. Discord Developer Mode → Copy Channel ID; use an owner-private channel and verify bot View/Send/Embed/Attach permissions. Channel `0` disables publication; do not silently reuse a possibly public old channel. Preserve existing API/Discord credentials.

- Verify remote merge, Pi pull and `make restart`, then `make status`/`make tail`.
- Verify Python 3.12+, real market-hours provider freshness, advancing files and an actual six-field delivered message—not just a heartbeat or a successful request.
- Check first two trading sessions for coverage, missed candidates, stale data, latency, delivery errors and flow availability. Use `!audit` or `python report_theta_audit.py --start YYYY-MM-DD --days 30`.
- Review weekly and before renewal. No automatic calendar reminder, subscription cancellation or licensed-data backup process has been set up.
- Live index collection/context storage continues after audit expiry; optional flow, request telemetry and audit mirrors stop. Billing continues. Preserve original audit start date on restart.

## Remaining research / operational work

1. Measure actual Pi data quality, accepted/fresh coverage and latency before interpreting outcomes. Absent telemetry is not proof of zero use; hung workers and partial flow coverage remain limitations.
2. Establish a private compliant retention/backup process for `.thetadata-data/context/`, `.thetadata-data/audit/` and any licensed research snapshots. Never commit or redistribute raw Theta/OPRA rows. Never hand-edit committed bot history.
3. Add explicit outcome-finalization timestamps in a future ledger schema; current historical reports conservatively use file mtime/heartbeat evidence. Copying files may make historical states unknown.
4. Evaluate optional Yahoo blocking and permanently hung provider calls; single-flight avoids worker multiplication, not hard timeouts. Investigate any live stalls before claiming reliable operation.
5. Evaluate the month against usage/quality and matched policy baselines without tuning on the same sample. Correlated SPX observations are not independent trade returns. Exact-contract bid/ask, costs and held-out multi-regime replay are required for option-profitability claims.
6. Decide keep/downgrade/replace only after evidence. Initial official-page research found Theta Options Value $40 versus Standard $80, but endpoint/entitlement compatibility and exact Index-tier pricing were not verified. Alpaca Basic is not equivalent consolidated/OPRA coverage; Tradier real-time access requires a brokerage account. Recheck current terms/prices rather than acting on preliminary figures. No cancellation/vendor move has been selected.

## Documentation map

- `docs/spx-notifications.md`: current message/action contract, migration and limitations.
- `docs/research/theta-subscription-audit.md`: audit/renewal protocol.
- `docs/state-of-the-code.md`: implementation architecture and historical modules.
- `docs/strategy-log.md`: frozen hypotheses and prior negative evidence; check before proposing strategy changes.
- `docs/next-steps.md`: earlier research roadmap; this handoff overrides its immediate deployment/removal priorities.

User also asked why vi behaved oddly. Offered `nano .env` (Ctrl+O, Enter, Ctrl+X), vi insert-mode explanation and safe exit; no specific terminal malfunction was diagnosed. Never request screenshots exposing credentials.
