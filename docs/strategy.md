# Rule contract: `spy-orb-reversal-v2`

Source of truth: `spy_setup/signals.py`. All prices and invalidation conditions refer to **SPY**, with manual SPX 0DTE execution. Correlation is not a price conversion or an option-return model.

## Opening-range breakout sequence

1. Build the 09:30–09:45 ET opening range from contiguous completed 1-minute OHLCV bars. Candle timestamps denote starts; decisions use closes.
2. Require a genuine close crossing outside the range, session VWAP alignment, and relative volume ≥1.2. Relative volume divides the current minute's volume by the median for that ET minute across the five preceding trading sessions, with at least three positive samples.
3. Emit **forming / breakout**, not an entry. A later candle must retest and hold the boundary within a tolerance of 10% of opening-range width, with VWAP alignment.
4. Emit **forming / retest**. The continuation trigger is the retest high +$0.01 for calls, or low −$0.01 for puts. Invalidation is retest low −$0.01 for calls, or high +$0.01 for puts.
5. A later completed candle must close through the trigger with VWAP and relative-volume confirmation. Emit **triggered** using that close as the entry reference—not an assumed earlier intrabar fill. Target is 2R from that reference to invalidation.
6. Emit **invalidated**, **target reached** or **expired** as appropriate. On ambiguous subsequent candles, invalidation wins. The trigger candle cannot retrospectively claim a target fill.

## Complementary failed-breakdown bullish reversal

This is an additional CALL setup, not an automatic reversal of a stopped PUT and not a bottom-picking rule:

1. After the opening range completes, observe a timely completed close at least $0.01 below its low. A wick alone and a late backfilled breakdown do not arm it. This contextual downside break does not itself need volume or VWAP confirmation or a qualifying PUT setup.
2. While no setup is active, require a **later** completed close at least $0.01 above the opening-range low, above session VWAP, with the same ≥1.2x relative-volume evidence. This is the first *qualified reclaim* after the break; it need not be the immediately adjacent crossing candle. It may occur after a PUT invalidation, but never on that same invalidation candle. An ordinary qualifying opening-range breakout takes priority if both could qualify.
3. Emit **forming / breakout**, labeled **Failed-breakdown reversal**, with `kind=failed_breakdown_reversal`. The reused phase means the first confirmation, not that the upper opening boundary broke. Initial invalidation is the opening-range **low minus** 10% of range width.
4. A separate later candle must retest the opening-range **low**, reaching within 10% of range width, closing at least $0.01 above that low and above VWAP. The initial invalidation must not have been touched. Emit **forming / retest**; trigger is retest high +$0.01 and invalidation is retest low −$0.01.
5. A separate subsequent completed candle must close through that trigger, above VWAP, with ≥1.2x relative volume. The close is the entry reference and 2R is the SPY target. No same-candle entry on reclaim or retest, and no trigger-candle target claim.

The observed breakdown remains eligible context until the normal noon cutoff; each subsequent timely downside close updates its timestamp. There is no added arbitrary sweep-age threshold. The bullish daily slot is shared with ordinary CALL breakouts: **one bullish setup total per session**, even if the first fails before entry. Single-active-setup and invalidation-first rules apply across both kinds. VWAP confirmation and a required retest deliberately sacrifice early/V-shaped entries; this addition is not promised to catch a particular rebound.

## Shared timing and safety

Pending setups expire 15 minutes after breakout/reclaim or at noon ET, whichever occurs first. No fresh entries at/after noon. Active setups expire five minutes before the exchange-calendar close, including early closes. Expiry uses the wall clock even without a new candle and records the exact deadline, never an assumed price or option exit. A completed candle ending exactly at the deadline is eligible: invalidation first, then target, then expiry; candles ending later cannot change the expired setup. With stale data, expiry remains informational (`available=false`, `fresh=false`), so existing collection/delivery safeguards suppress actionable levels and notifications. A missing feed therefore does not guarantee an expiry notification; it never extends setup validity. There is at most one setup per direction per session and only one active setup at a time.

## Causality and delivery

- History and current candles are receipt-gated. Downloaded history cannot retroactively originate an earlier setup. Late current bars may provide context but cannot originate stale actionable transitions.
- Missing/invalid current bars or stale data produce unavailable status, not a fabricated directional signal. Zero volume is retained honestly; insufficient volume evidence prevents qualification.
- Snapshot candle time and lifecycle event time are distinct. Only the current lifecycle transition may be published, within 90 seconds of its close, using durable identity/delivery safeguards. The replay event list is not a notification queue.
- SPX strikes, option premiums, slippage, exercise/settlement risk and returns are not inferred. Fresh SPX option quotes and spread/liquidity checks would be prerequisites for a future contract-selection feature.

## Version and deployment

The new semantics use `spy-orb-reversal-v2`; existing `spy-orb-retest-v1` state must not be relabeled or rewritten. The current date-keyed store rejects a same-day ledger from another version. Deploy before a fresh trading session with no existing ledger for that date, or implement a separately reviewed version-aware state handoff; a mid-session restart over a v1 ledger will fail closed. Preserve all historical state and first-seen receipts. Discord display remains Pacific (PST/PDT); all rule times here are Eastern.

## Independent informational notifications: `spy-informational-v2`

`spy_setup/informational.py` calculates separate opening-range and Bollinger Band events; these do **not** modify or imply the setup rules above. The 15-minute opening-range announcement and 1m close crossings need no VWAP, relative-volume or retest confirmation. The BB notifications compare completed 5m/10m closes against their own rolling 20-close SMA ±2 population standard deviations, requiring a previous inside/on-band close and a current strictly outside close. Only outward crossings notify, not touches or reentries.

Five- and ten-minute bars are regular-session aligned, with every constituent minute required. No overnight/prior-session warm-up: first possible 5m/10m BB crossings are 11:15/13:00 ET (08:15/10:00 Pacific). Ten-minute BBs cannot notify on 13:00 ET early closes; the first eligible close is already outside the send window. Thirty-minute BBs are not enabled: the required 21 candles span 630 minutes, beyond the 390-minute regular session. One-minute opening-range crossings remain unchanged; one-minute BBs are removed. No extra historical collection is introduced. Version v2 uses a separate `informational-v2` journal directory and current-version delivery counts; preserve v1 journals without migration. Switch between sessions to avoid new-version IDs notifying still-fresh range events already observed under v1. Original receipts, contiguous session coverage, the 90-second event freshness window and regular market hours gate all notifications. No setup noon cutoff applies. Separate durable journals prevent duplicates and replay; failed or uncertain sends are not automatically retried. See README for operational details and the deliberate risk of missing notifications during outages.

## Limitations

These thresholds are transparent starting conventions, not a validated edge. Feed-specific Nasdaq Basic VWAP/volume may differ from a consolidated feed or a trading platform. Closed-candle references are not executable fills. SPY structure can diverge from SPX, and SPX options can lose value despite the expected underlying direction. Test this new strategy independently before relying on alerts.
