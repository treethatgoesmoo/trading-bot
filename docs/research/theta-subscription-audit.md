# Theta subscription audit — SPX context and frozen ORB

## Purpose and activation

The owner trades SPX 0DTE manually through Robinhood and wants underlying context, not automated option execution. Use the already-paid subscriptions for a fixed **30-calendar-day** evidence window, then assess renewal using utilization, data quality, and incremental setup evidence—not request volume alone.

**Status: implemented for review on `feat/spx-context-actions-audit`; Pi deployment is not verified.** Follow the [SPX migration guide](../spx-notifications.md). The Pi already has the repository cloned; once these changes reach remote `master`, pull `master`, preserve/update the existing `.env`, and run `make restart`.

```env
NOTIFICATION_MODE=spx
SPX_CONTEXT_CHANNEL_ID=123456789012345678
THETA_AUDIT_START_DATE=YYYY-MM-DD
THETA_AUDIT_DAYS=30
SPX_CONTEXT_FLOW_ENABLED=1
SPX_CONTEXT_PROXY_ENABLED=1
```

Replace the example channel with a verified private owner channel and the date with a fixed ET deployment date. Preserve the real Discord/Theta credentials. Channel `0` disables publication without a legacy fallback. `THETA_AUDIT_FLOW_ENABLED` is **legacy-only**; use `SPX_CONTEXT_FLOW_ENABLED=0` to opt out of replacement-mode options sampling.

The audit window is `[start, start + days)`. Restart does not reset it. Blank/invalid start disables telemetry and optional replacement-mode flow, **not live SPX index collection**. After expiry, live context/index collection continues; options sampling, new request metadata, and audit mirrors stop. An admitted request may append its completion after expiry. **Vendor billing is never cancelled automatically.** Review before the actual renewal date.

## Three separate accounting questions

1. **What was requested?** `usage-YYYY-MM-DD.jsonl` pairs SDK starts/finishes by request ID: endpoint, product, caller purpose, timestamps, duration, success/error class, and raw row count. It excludes credentials, request arguments, response payloads, and exception text. SDK-internal retries are not separately counted; raw rows can overlap and are not unique useful observations or billing units.
2. **What was incorporated?** `context-YYYY-MM-DD.json` records source use/status, accepted-bar provenance, representative acquisition IDs, and constituent consumed IDs for every assessment. `spx_context` is operational index demand; `spx_context_flow` is optional experimental options demand. Reusing cached data does not multiply consumed requests. An empty refresh is distinct from incorporating returned trades. Unmatched/missing IDs remain visible.
3. **What reached the owner, and what followed?** The report separates all assessments from sent notifications and retains suppressed/failed/pending/unknown states. Sent observations receive causal 15/30-minute SPX forward outcomes. Neutral observations have no invented correctness score; overlapping observations are not independent trades. Source incorporation is **not proof of incremental predictive value**.

Yahoo SPY proxy use is labeled separately: indicative, optional, and not part of the Theta invoice. It does not establish executable SPX volume or real-time completeness.

## Action accounting is not option profitability

The frozen experimental `spx-action-retest-v1` checklist adds Action to the same assessments, without extra provider requests. Recorded version/reason/source roles distinguish SPX rule inputs from contextual-only prior levels, SPY and options flow. Incorporating flow in the description does **not** mean flow affected the action.

The report separately counts delivered action states and summarizes validated 15/30-minute SPX movement for call/put candidates only. Wait/no-trade states receive no directional action score or avoided-loss credit. Legacy rows without actions and invalid/unknown action versions remain unclassified. Pending/missing/stale-delivery/close-censored outcomes are visible, not repaired by replay. These overlapping paper observations neither validate executable option returns nor measure flow's incremental action value. See the [exact checklist](../spx-notifications.md#experimental-action-checklist).

## Frozen ORB comparison remains separate

Rule identifier: `spx-orb-15m-first-break-v1`. Notification retest/holding descriptions do not change this rule.

- Require all completed 09:30–09:44 ET SPX minute bars and uninterrupted minutes through the candidate.
- Take only the first subsequent close strictly outside the range, with the close and decision receipt before 11:00 ET. Last eligible bar starts 10:58.
- At most one candidate per session, including missed candidates. Observe within 90 seconds of close or mark missed; never select a more convenient later breakout.
- Model entry at the next minute open strictly after decision availability. Stop at the opposite range boundary; target 1R; maximum hold 30 minutes.
- Ambiguous stop/target bars resolve stop-first. Adverse stop gaps use the open; favorable target gaps are capped. Missing/invalid entry or outcome evidence remains explicit.
- Price-only takes every eligible candidate. The flow-filtered policy retains only candidates with usable aligned flow of at least 0.15 premium ratio.
- Usability requires cache and latest-trade age no more than 120 seconds, at least 25 classified prints and at least 20% classified coverage. Missing flow is neither a veto nor evidence against the options subscription.

Compare both policies on the **same completed evaluable-flow candidate sessions**. Filtered candidates contribute zero R to the filtered policy; missing flow/incomplete outcomes are excluded and separately counted. Also report all price-only results. The notification's price-derived direction is not changed by flow; the ORB policy is the separate incremental-flow experiment.

One SPX collector feeds both context and this experiment. The audit does not add a duplicate current-session index fetch. The old full turning-point watch and option-contract lookup are not enabled by replacement mode.

## Reports and review cadence

```bash
python report_theta_audit.py --start YYYY-MM-DD --days 30
# Inclusive completed Eastern date; default cutoff is the current instant:
python report_theta_audit.py --start YYYY-MM-DD --days 30 --as-of YYYY-MM-DD
```

`!audit` provides the same offline summary in the private context channel. `--directory PATH` supports privately copied evidence. Reports are read-only, make no provider calls, and do not rewrite finalized missing outcomes. Current sessions are partial. Schema-1 terminal outcomes lack finalization timestamps; historical cutoffs can conservatively produce unknown states, especially after copying files changes modification times.

- **First two sessions:** verify paired request records, advancing collection, actual delivered messages, timestamps, stale/missing sources, and no old automatic streams. A configured startup message is not successful capture or publication.
- **Weekly:** review coverage, failed/empty/slow requests, used versus fetched data, missed/unknown delivery, missing outcomes, and paired ORB results. Do not tune thresholds to the latest week.
- **Before renewal:** review the full window, actual invoice split, replacement endpoint entitlements, and operational dependence. Thirty calendar days generally permit only about twenty ORB candidates at most—not enough to establish durable profitability.

## Renewal gates

| Evidence | Decision implication |
|---|---|
| Absent telemetry, corrupt files, or major gaps | Diagnose configuration, downtime, writes and provider behavior first. Absence is not zero usage or strategy failure. |
| Reliable SPX prices support timely context | Preserve reliable index access while comparing cheaper tiers/providers on the exact history, freshness and endpoint requirements. |
| Options used only experimentally, with poor availability or no repeatable incremental ORB benefit | Options Standard has not earned a recurring role yet; consider pausing/downgrading after checking other account uses and replacement compatibility. |
| Apparent improvement concentrated in a few days or one chronological half | Inconclusive; do not promote a strategy or justify indefinite renewal from this alone. |
| Consistent promising paired results with adequate capture | Consider a pre-budgeted extended study and untouched validation—not a claim of option profitability. |
| Price-only ORB baseline itself performs poorly | Reconsider the setup hypothesis. Improving a losing baseline does not automatically make the filtered policy tradable. |

Before strategy promotion, validate the same fixed rules across multiple market regimes with chronological holdouts. The retired ensemble/break-and-retest system still lacks an honest replay harness. Exact SPX 0DTE option returns additionally require executable bid/ask, spreads, fees, entry/exit timing, and IV/decay evidence. Neither notification grading nor SPX points/R supplies those costs.

## Storage and validation boundary

- `.thetadata-data/context/context-YYYY-MM-DD.json`: continuing licensed local SPX evidence, receipts, source provenance, delivery ledger and outcomes.
- `.thetadata-data/audit/usage-YYYY-MM-DD.jsonl`: bounded request metadata.
- `.thetadata-data/audit/context-YYYY-MM-DD.json` and `orb-YYYY-MM-DD.json`: bounded mirrors from the same collector.
- `.thetadata-data/shadow/`: retained legacy full-watch evidence; not silently pooled with the new results.

Keep all licensed records private, ignored, and out of Git, tracked `data/`, public channels, and raw Discord attachments. Use a private license-compliant backup; no automatic retention deletion/backup is added. Do not run two bot processes against the same session files.

Offline tests exercise synthetic timing, gaps, stale data, delivery, provenance, report denominators, and expiry. They do not validate live SDK entitlement, Pi availability, Discord permissions, feed completeness, savings, or a profitable strategy. Run the repository checks in `README.md` before deployment and verify live evidence afterward.
