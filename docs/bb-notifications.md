# BB event notifications — current implementation

This local implementation replaces the former SPX structure/action indicator and removes all legacy registration/rollback paths. It continues tracking **SPX**, not SPY or a selected option contract. No deployment, paid AI integration or predictive validation is implied.

## Message contract

`zero_dte/bb_signals.py` defines `bb-events-v1`: BB(20,2, population standard deviation), shared Wilder-style RSI(14), MACD(12,26,9), minimum 35 completed candles per timeframe. Every event message includes 1m/5m/15m indicator context, close and band levels, RSI/change, MACD histogram/change and completion timestamps. Unavailable frames explain why. No Action/consider-calls/puts, opening-range narrative, confidence percentage, proxy or flow section remains.

| Event | Completed-candle definition |
|---|---|
| Upper/lower contact | High/low reaches its own completed band after a preceding non-contact candle. |
| Close outside | Close is strictly outside after the preceding close was not outside that side. |
| Rejection | Fresh wick contact closes strictly inside, or a prior outside close returns strictly inside. |
| Continuation | Second consecutive outside close; third and later outside closes do not repeat the event. |

Both sides are symmetric. A flat zero-width band produces no contacts. A single candle may generate multiple related event labels, and simultaneous timeframe events are grouped in one message. These are descriptive events, **not a rule to fade touches or buy continuation**. MACD weakening can coexist with a positive histogram; no arbitrary combined score disguises that distinction.

Contacts use the **completed candle's bands**, not reconstructed intrabar band values. Minute polling therefore detects completed evidence, not exact tick contact times. The latest completed 5m/15m state stays visible. Each event remains eligible for 90 seconds after its own candle end, allowing recovery after a skipped boundary poll; persistent IDs prevent reissuing delivered events on intervening polls. Separate new 1m events can still produce frequent messages; persistent deduplication does not guarantee a low daily event count.

## Data, timing and history

One ThetaData SPX current-session minute collector feeds all three timeframes and the unchanged quiet ORB experiment. Complete, contiguous session-open-aligned candles only; no unfinished higher-timeframe candle, missing minute, invalid OHLC or future receipt can contribute actionable-looking current evidence. A fresh last minute is no more than 90 seconds old measured from its end.

Two preceding trading sessions are requested separately, cached and retried independently at most every 15 minutes when incomplete. Complete sessions are not repeatedly fetched, including after restart. Optional history is accepted as an unbroken chain from the immediately previous trading session; an incomplete older session does not poison complete newer history. Candles never bridge overnight gaps; indicator sequences include the overnight price jump. The fixed history window/initialization may differ from charts with deeper history.

Two normal prior sessions provide 52 fifteen-minute candles, allowing 15m indicators at **09:45 ET**. Missing history or insufficient shortened sessions stays `warming_up`. Current evidence is saved before optional historical requests can block. The first poll assesses current evidence before fetching history; cached history feeds the next poll and cached status.

Collection runs after 09:31 ET and allows five minutes after regular/early close to capture the last bar. Publication is market-hours only. There is no opening-range gate, periodic heartbeat, or generic no-trade post. Provider failures circuit-break after three failures for five minutes. Single-flight thread ownership prevents overlapping SDK requests even under cancellation; a permanently stuck SDK request can still stall collection/shutdown.

## Delivery and commands

- Only `SPX_CONTEXT_CHANNEL_ID` is used. It retains its old name for upgrade compatibility. `0` means no publication; `ALERT_CHANNEL_ID` is never a fallback.
- `!bb` and `!status` display cached indicators without provider requests, even while collection is slow. They are not new event deliveries and are not counted as trades.
- `!audit` shows current-session request attempts/results, sent/failed/unknown/suppressed/pending message counts, sent event counts, consumed request IDs and frozen ORB status. BB outcomes explicitly remain **not evaluated**.
- Shared commands are `!cmd`/`!cmds` and owner-only, private-channel `!commit`. Old `!spx`, `!events`, forecast/swing, dashboard, stats and flow commands are not registered.

Evidence lives in `.thetadata-data/bb-events/bb-events-YYYY-MM-DD.json`, separate from retired context files. Stable event IDs are saved with publication intent before sending. Interrupted pending sends become unknown on restart. Explicit Discord rejection or a missing channel cache may retry while the original event remains fresh; ambiguous network failures do not automatically retry. This trades some missed delivery for duplicate avoidance, not exactly-once guarantees. Each included event's age is checked again before sending, so a fresh 1m snapshot cannot prolong an older event. If any member of a grouped message expires before send/retry, the entire original group is suppressed rather than rewritten; this can miss a younger member. Delayed acknowledgements are marked stale. Channel-ID checks do not verify private membership or permissions.

## Audit preservation and boundaries

- Telemetry labels the new provider use `bb_events`. Only accepted history and available indicator inputs receive consumption credit; stored but unusable history does not.
- Existing same-day frozen ORB evidence is adopted from validated old live context and/or the audit mirror, retaining first-seen receipts and available request lineage. If both exist, their records must match exactly; conflicting/corrupt sources fail closed before requests or writes. Even a legitimately stale mirror requires explicit reconciliation—do not delete evidence to force startup. Audit-active mirrors remain under `.thetadata-data/audit/`; no ORB notifications are emitted.
- No options-flow sampler or Yahoo SPY collector is installed. New flow-filtered comparisons consequently lack new flow evidence. Index and options subscriptions are not automatically changed or cancelled.
- `report_theta_audit.py` continues reporting existing telemetry and historical context/ORB evidence; it does not interpret BB snapshots as old action candidates. Use `!audit` for current BB delivery counts.
- The explicit original audit start date and duration remain unchanged. Expiry stops request telemetry and mirrors, not live index requests or billing.
- Old context/action engines and historical cogs remain unregistered for historical tests/readers; they cannot be enabled through `.env`. Their old historical grading is not reused to invent BB performance.

## Upgrade and validation

Preserve the existing Pi `.env`. Keep the real Discord/Theta credentials, private channel, and original audit date. `SPX_CONTEXT_POLL_SECONDS=60` remains supported. Remove obsolete `NOTIFICATION_MODE`, old feature flags, flow/proxy flags and old heartbeat/cooldown settings at convenience; they cannot revive notifications. New BB parameters are frozen code conventions, not old `BB_PERIOD`/`BB_STD` env knobs.

After review and merge to remote `master`, pull and `make restart`; then inspect `make status` and `make tail`. Verify Python 3.12+, fresh licensed minute files, warm-up completion, exact chart settings, an actual event message, duplicate suppression, stale `!status`, channel restriction and no retired posts. Do not create a public data channel or expose credentials in screenshots.

Offline tests cover event symmetry, multi-timeframe completion/causality, warm-up, holidays/early closes, gaps/staleness, persistence, deduplication, ambiguous sends, request provenance, cancellation single-flight, command boundaries, real cog assembly and frozen ORB adoption. None establish edge. No held-out BB event study, executable options backtest, live provider call or Pi smoke test has been completed for this change.
