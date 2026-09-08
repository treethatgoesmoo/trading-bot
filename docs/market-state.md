# Market-state contract — `spx-state-v1`

Implemented locally for the owner's completed-candle bullish/bearish/volatile Discord alerts. These are deterministic descriptive rules, not an optimized strategy, confidence model, option entry or forecast. The README is the feature/configuration registry; this page defines exact semantics and limitations.

## Data and timeframes

- One SPX minute source, minute-start timestamps, timezone-aware UTC storage and ET presentation.
- Only completed, valid positive OHLC bars received by computation time. The request cutoff prevents response latency from making a partial requested candle appear complete.
- First-seen prices and receipt timestamps are immutable. Duplicate/corrupt observations cannot supply missing evidence; a gap in the current session prevents classification.
- Aggregate complete non-overlapping regular-session 1m/5m/15m candles aligned to 09:30 ET. Respect holidays and early closes. Never aggregate across sessions.
- Indicators include up to two complete immediately preceding sessions, in an unbroken chain: never skip a missing immediate session to use older data. Candles stay session-separated, but indicator series and true ranges include overnight changes.
- BB uses a 20-close SMA and ±2 population standard deviations; RSI uses the shared 14-period implementation; MACD uses EMA(12) minus EMA(26) with signal EMA(9). The histogram is MACD minus signal, not histogram change.
- At least 35 candles for displayed RSI/MACD/BB; this is a fixed warm-up convention, not proof of EMA convergence. Volatility needs 40 candles, and evaluating the previous and current 5m candles requires 41 total 5m candles.
- **1m:** display only. **5m:** state evaluation. **15m:** directional context. No weighted votes or three-timeframe agreement score.

## Direction

First compute a raw condition for each of the latest two current-session 5m candles:

| Input | Bullish condition | Bearish condition |
|---|---|---|
| 5m close versus BB middle | Strictly above | Strictly below |
| 5m RSI | ≥55 | ≤45 |
| 5m MACD histogram | >0 | <0 |
| Contemporaneous completed 15m close versus BB middle | Strictly above | Strictly below |
| Contemporaneous completed 15m RSI | ≥50 | ≤50 |
| Contemporaneous completed 15m MACD histogram | ≥0 | ≤0 |

Every condition in a column must hold. A finite, available set that qualifies for neither is raw neutral. Undefined/missing inputs are unavailable, not neutral.

Each 5m candle uses only today's 15m candles that had completed **by that particular 5m candle's end**. For example, at 10:00 the preceding 09:50–09:55 candle cannot use the 09:45–10:00 context. It uses the context available at 09:55 instead. Overnight context cannot substitute for today's first 15m close.

- Two consecutive raw bullish conditions establish **bullish**.
- Two consecutive raw bearish conditions establish **bearish**.
- Otherwise the direction is **neutral**.
- If required data is unavailable, classification is unavailable rather than clearing a prior bias.

This is confirmation from completed prices available at computation time, not a claim that we sent or observed a prior alert. With sufficient prior history, the earliest classification is **09:50 ET**: the 09:40–09:45 and 09:45–09:50 candles both have today's completed 15m context. At 09:45, the previous 5m candle does not.

## Independent volatility

For each assessed 5m candle:

```text
TR = max(high - low, abs(high - preceding close), abs(low - preceding close))
BB width = (upper - lower) / middle
```

A raw volatility trigger is either:

1. TR ≥ **1.5 × median of the preceding 20 completed 5m true ranges**, or
2. BB width ≥ **1.5 × median of the preceding 20 completed 5m widths**, AND width is strictly greater than the previous width.

The assessed candle is excluded from both baselines. These thresholds are chosen implementation conventions, not thresholds selected from profitable backtests. A zero baseline and zero current value is quiet. Positive movement against a zero baseline is unavailable, not an infinite ratio or an invented volatility signal.

The current state is volatile if **either of the latest two current-session 5m candles** triggers. Therefore volatility clears after two quiet candles. It can coexist with bullish, bearish or neutral direction. Disagreement between RSI/MACD/timeframes alone does not establish volatility. If required direction or volatility evidence is unavailable, the combined classification is unavailable.

## Notification lifecycle

- Initial neutral/nonvolatile observation: record silently.
- Initial directional or volatile observation: alert.
- Changed direction or changed volatility: alert, including one neutral clearing or volatility-ended transition.
- Same direction and volatility: record the new completed 5m observation without another alert.
- Unavailable/stale: do not manufacture a transition or authorize an old retry.

Each observation is immutable and keyed by version/session/5m start/direction/volatility. Assess at most once per fresh decision candle. The transition baseline is **last observed state**, not last successful send. A suppressed/unknown transition is not replayed on restart or when publication is enabled; users can request status, and a later genuine change can alert.

Publication requires enabled collection, configured credentials/client, enabled publication, configured private destination, fresh minute evidence and a decision no more than **90 seconds after its own 5m end**. A fresh 1m poll never refreshes an old decision. This allows a skipped boundary poll to recover a still-fresh state, without historical catch-up alerts.

Regular-session publication only. Collection continues for up to five minutes after close to preserve bars, but there are **no post-close state alerts or fresh classifications**. The final 5m candle normally arrives after close and is consequently not in the fresh-observation ledger. Offline review must not present that ledger as complete session coverage.

### Delivery evidence

- Save intent before sending; recheck freshness after the save.
- Confirmed channel/permission failures may retry while the original observation is fresh and still current.
- Expired or superseded attempts become suppressed.
- Network/timeout/server ambiguity and cancellation become unknown; never automatically retry them.
- Interrupted attempted intents recover as unknown; unattempted intents recover as suppressed.
- Successful acknowledgement after expiry is retained as `sent` with reason `delivered_stale`.
- A write failure invalidates memory; no further send or collection mutation is allowed before durable reload.

No exactly-once delivery claim: avoiding duplicate sends can mean missing an alert with an uncertain acknowledgement.

One process owns the evidence directory through an OS lock acquired before the monitor starts. It releases ownership only after the monitor and outstanding shielded provider worker stop. Never delete the stable lock file while a service may be alive. The lock does not coordinate an old retired bot or multiple machines; stop old processes before upgrade.

## Status and recomputation

`!status` (`!bb` alias) uses cached evidence without collection, migration or waiting behind the network lock. Only the configured private channel receives indicators. It shows source, candle age, readiness, requests, deliveries and requested versus effective publication. No `!audit`, `!commit` or old feature commands remain; `!help` is the built-in command help.

Newly acquired prior history can change indicator initialization for the same completed candle. The recorded observation is not revised or re-alerted. If current recomputation differs, status explicitly labels it as a recomputation, displays the recorded state and identifies the last acknowledged state. Completed candles are not a guarantee of immutable results when history changes.

## Storage and migration

New `schema=1`, `version=spx-state-v1` ledgers use `.thetadata-data/market-state/`. They store neutral bars, prior-session retry metadata, request lineage, snapshots, immutable observations and separate delivery attempts. Atomic writes use fsync and replacement; POSIX also syncs the directory. Corrupt versions, duplicate keys/IDs, contradictory metadata, nonfinite numbers or incompatible evidence fail closed.

On an absent new same-day ledger:

1. Prefer a validated `bb-events/bb-events-YYYY-MM-DD.json` source.
2. If none exists, inspect `context/context-YYYY-MM-DD.json` and `audit/orb-YYYY-MM-DD.json`. If both exist, their records must agree exactly.
3. Preserve bars, first-seen receipts, available request lineage and applicable warm-up data. Record original source paths and SHA-256 hashes. Start new-service request/poll counters at zero.
4. Do not import old actions, candidates, outcomes or deliveries as current state alerts. Do not grade or reconcile ORB. Original files remain untouched.

The read-only review tool validates existing current ledgers directly and never runs migration or changes pending records. Its optional CSV includes all recorded observations, not just successful deliveries. Outcomes remain **not evaluated**.

## Validation boundary

Automated checks cover thresholds/symmetry, two-candle persistence, independent volatility, causal higher-timeframe context, unavailable data, warm-up, holiday/early-close behavior, skipped polls, restart deduplication, send ambiguity, disk failures, migration, ownership and the real command/configuration boundary. Synthetic tests do not prove real-feed parity, private channel permissions, Pi runtime behavior, usefulness or option profitability. Any rule change needs a new version and separate evidence.
