# Initial rule contract: `spy-orb-retest-v1`

Source of truth: `spy_setup/signals.py`. All prices and invalidation conditions refer to **SPY**, with manual SPX 0DTE execution. Correlation is not a price conversion or an option-return model.

## Sequence

1. Build the 09:30–09:45 ET opening range from contiguous completed 1-minute OHLCV bars. Candle timestamps denote starts; decisions use closes.
2. Require a genuine close crossing outside the range, session VWAP alignment, and relative volume ≥1.2. Relative volume divides the current minute's volume by the median for that ET minute across the five preceding trading sessions, with at least three positive samples.
3. Emit **forming / breakout**, not an entry. A later candle must retest and hold the boundary within a tolerance of 10% of opening-range width, with VWAP alignment.
4. Emit **forming / retest**. The continuation trigger is the retest high +$0.01 for calls, or low −$0.01 for puts. Invalidation is retest low −$0.01 for calls, or high +$0.01 for puts.
5. A later completed candle must close through the trigger with VWAP and relative-volume confirmation. Emit **triggered** using that close as the entry reference—not an assumed earlier intrabar fill. Target is 2R from that reference to invalidation.
6. Emit **invalidated**, **target reached** or **expired** as appropriate. On ambiguous subsequent candles, invalidation wins. The trigger candle cannot retrospectively claim a target fill.

Pending setups expire 15 minutes after breakout or at noon ET, whichever occurs first. No fresh entries at/after noon. Active setups expire five minutes before the exchange-calendar close, including early closes. Expiry uses the wall clock even without a new candle and records the exact deadline, never an assumed price or option exit. A completed candle ending exactly at the deadline is eligible: invalidation first, then target, then expiry; candles ending later cannot change the expired setup. With stale data, expiry remains informational (`available=false`, `fresh=false`), so existing collection/delivery safeguards suppress actionable levels and notifications. A missing feed therefore does not guarantee an expiry notification; it never extends setup validity. There is at most one setup per direction per session and only one active setup at a time.

## Causality and delivery

- History and current candles are receipt-gated. Downloaded history cannot retroactively originate an earlier setup. Late current bars may provide context but cannot originate stale actionable transitions.
- Missing/invalid current bars or stale data produce unavailable status, not a fabricated directional signal. Zero volume is retained honestly; insufficient volume evidence prevents qualification.
- Snapshot candle time and lifecycle event time are distinct. Only the current lifecycle transition may be published, within 90 seconds of its close, using durable identity/delivery safeguards. The replay event list is not a notification queue.
- SPX strikes, option premiums, slippage, exercise/settlement risk and returns are not inferred. Fresh SPX option quotes and spread/liquidity checks would be prerequisites for a future contract-selection feature.

## Limitations

These thresholds are transparent starting conventions, not a validated edge. Feed-specific Nasdaq Basic VWAP/volume may differ from a consolidated feed or a trading platform. Closed-candle references are not executable fills. SPY structure can diverge from SPX, and SPX options can lose value despite the expected underlying direction. Test this new strategy independently before relying on alerts.
