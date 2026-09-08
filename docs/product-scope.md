# Product decision: private SPX state alerts

## Confirmed scope

The owner requested a full cleanup after repeated feature changes, then specified:

1. "lets do bearish, volatile, bullish alerts"
2. "completed candle"
3. "yes discord is essential. it should give me alerts"

Implementation direction: **one private SPX market-state alert service for Discord**. Direction and volatility are separate observations, not option-entry recommendations. The owner subsequently requested implementation. Current features, controls and deployment instructions are in the [README](../README.md); exact versioned rules are in [the market-state contract](market-state.md).

The implementation uses 5m decisions, completed 15m directional context and 1m display context. Thresholds and persistence were selected as explicit, testable starting conventions—not as user-supplied values or demonstrated optimal rules.

## Keep / remove / separate

- **Keep live:** one SPX source, reliable completed bars, one state engine, one private Discord destination, cached status, durable observations/delivery outcomes and minimal operational counters.
- **Remove:** old cogs and notification UI, ORB computation, swing/forecast/flow/watchers, economic notices, auto-tuning, automatic Git synchronization, legacy strategy settings and provider fallbacks.
- **Separate:** selected manually invoked research/readers and their dependencies under `research/`. Preserve historical evidence and interpretation; do not preserve a second runnable bot.
- **Review:** a small read-only operational report and optional owner-feedback CSV. Not a dashboard, journaling application or predictive-performance claim.
- **Out of scope:** automated orders, contract selection, positions/exits, AI-generated signals/explanations, confidence percentages and subscription changes.

No plugin framework or strategy menu. A feature must be live, explicitly invoked research, or removed—not silently enabled by a forgotten flag. Old source remains recoverable through Git history.

## Research behind the decision

Official sources were retrieved during the product investigation. Vendor descriptions are not independent performance evidence.

- [TradingView alerts](https://www.tradingview.com/pine-script-docs/concepts/alerts/) run server-side and snapshot script settings. [Repainting guidance](https://www.tradingview.com/pine-script-docs/concepts/repainting/) distinguishes completed context from unconfirmed higher-timeframe values. Ordinary chart alerts are a credible alternative to custom calculations, but **native chart-only delivery does not meet the clarified Discord requirement**.
- [TradingView webhooks](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/) require 2FA, can fail and have a three-second receiver timeout. A relay adds another service/security boundary. Do not add it alongside a second live collector by default.
- [Bollinger's rules](https://www.bollingerbands.com/bollinger-band-rules) warn that band tags alone are not buy/sell signals, prices can walk a band, and related momentum measures are not independent confirmation votes. This supports descriptive states rather than a fabricated confidence score.
- [TrendSpider](https://trendspider.com/) advertises multi-timeframe analysis, Strategy Tester, Sidekick and ML Quant Lab. Exact SPX history/entitlements and independent usefulness were not verified. No purchase or integration is justified yet.
- [QuantConnect's research guide](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide) supports a fixed hypothesis and untouched out-of-sample evaluation. Research must not continuously retune the production notifier against recent outcomes.
- [Cboe's 0DTE resources](https://www.cboe.com/tradable-products/0dte/) explain near-expiry sensitivity. Completed-candle observations are not an intrabar entry/exit or position-risk service; underlying moves do not establish executable option P&L.
- [Theta pricing](https://www.thetadata.net/pricing) and [terms](https://www.thetadata.net/terms-and-conditions) require account-specific entitlement and permitted-use checks. Active index-only use may make an Options plan unnecessary for this service, but retained explicit research can still need it. No subscription was changed.

No current total-cost comparison, account entitlements, live trial or profitable indicator combination was established. The earlier [BB research memo](research/bb-multitimeframe-and-ai-tools.md) preserves historical diagnostics and limitations; those SPY samples do not validate this SPX state rule.

## How to judge the product

Proposed next step after verified deployment: a **10 trading-session usability pilot**, without automatically extending the paid audit window. Agree acceptable alert burden and cost before starting.

Measure separately:

1. **Operations:** data readiness, delays, failed/unknown/suppressed deliveries, duplicates and outages.
2. **Owner usefulness:** worthwhile chart check, redundant, late or irrelevant. Include unchanged/silent recorded states when reviewing context, not only successful alerts.
3. **Predictive evidence:** a separate frozen study of subsequent movement and excursions, chronological holdout and session-level uncertainty. This is not implemented by the operational report.

Ten sessions can reveal workflow failures, not prove a profitable options strategy. If the custom workflow adds no value, simplify it or replace its source/delivery implementation rather than adding more indicators. Discord remains required unless the owner changes that decision.

## Implementation and remaining gates

Local code now separates neutral data/storage from historical ORB, deletes retired runtime surfaces, isolates live configuration, adds process ownership and versioned state delivery, and provides read-only review. Historical evidence remains intact. Runtime/source tests and historical-reader tests are separate.

Before deployment: review/merge, preserve and migrate the existing Pi configuration explicitly, verify Python/SDK, stop the old process, confirm private-channel/data permissions, and exercise live freshness and restart behavior. No live provider validation, Pi deployment, predictive evaluation or profitability claim follows from passing offline tests.
