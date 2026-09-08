# Research memo: multi-timeframe Bollinger events and AI tools

**Subsequent implementation:** the owner approved replacing the indicator. See [BB notifications](../bb-notifications.md) for the implemented observational stream; the research memo below predates that change. The held-out event study and predictive validation remain outstanding. No third-party AI service was added.

## Scope and conclusion

Owner proposal: note 1m/5m/15m Bollinger Band contacts with RSI and MACD rather than the current unhelpful signals. This memo assumes those numbers mean chart timeframes and SPX remains the target. No notification, trading, configuration, or deployment changes were made.

**Research priority, not a demonstrated ranking of predictive impact:** distinguish band rejection from continuation first; then test volatility/trend context, completed higher-timeframe states, and the incremental contribution of RSI and MACD. Neither public indicator descriptions nor this repository establish a profitable SPX/0DTE rule.

## Public-source findings

Pages retrieved during this investigation; product descriptions are vendor claims, not independent performance evidence.

1. [John Bollinger's rules](https://www.bollingerbands.com/bollinger-band-rules): a tag is not itself a buy/sell signal; price can walk a band in a trend; closes outside initially suggest continuation. He cautions against treating related indicators as independent confirmation. BandWidth describes relative width and supports studying volatility contraction/expansion. His guidance is not a validation of this specific intraday strategy.
2. [Fidelity RSI guide](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/RSI): RSI can remain overbought/oversold during strong trends. The guide describes different RSI ranges in rising/falling markets. Thus RSI >70 alone should not imply a short.
3. [Fidelity MACD guide](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/macd): MACD is primarily a trend/momentum tool and can whipsaw in ranges. Histogram sign, histogram change, zero-line location, and a fresh signal-line crossover are different features.
4. [QuantConnect research guide](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide): hypothesis-driven work, constrained experimentation, and untouched out-of-sample periods matter. Do not interpret its heuristic experiment-count thresholds as statistical guarantees.

### Interpretation for this bot

- **Contact is an observation; rejection or continuation is a later event.** Separate wick contact, completed close outside, close back inside, and repeated outside closes.
- **RSI and MACD are both price-derived momentum measures.** Their agreement is not two independent votes. Test each against the same BB-event baseline.
- **1m/5m/15m agreement also shares underlying observations.** Useful context is possible, but there is no evidence here that three aligned timeframes automatically outperform one.
- Reasonable initial architecture: 1m event timing, 5m setup context, 15m broader state. Treat this as a hypothesis, not an established optimum; do not require simultaneous contacts by default.
- Test both reversal and continuation symmetrically. Do not invert a poor reversal hit rate into a claimed continuation edge.

## Repository evidence

Read-only inspection and aggregation of tracked historical files found:

| Historical observation | Graded sample | Old grader's result |
|---|---:|---:|
| SPY 1m BB alerts | 33 | 13 marked correct (39.4%) |
| SPY 5m BB alerts | 74 | 28 marked correct (37.8%) |
| 15m RSI directional votes, schema 1 | 220 | 54.1%; same-row always-bullish baseline 49.5% |
| 15m MACD directional votes, schema 1 | 443 | 52.4%; same-row always-bullish baseline 48.8% |

Sources: `data/bb_alerts-*.jsonl`, `data/results-*.json`; grading definitions in `zero_dte/tracker.py`, votes in `zero_dte/strategy.py`.

These are descriptive legacy diagnostics, not validated performance. BB grading assumed mean reversion, used session-long subsequent 5m closes and asymmetric reference barriers, and did not model fills. The archive has no 15m BB events. Schema-1 forecast results use an older outcome definition; only six schema-2 rows exist, four with directional outcomes. Overlapping samples, a short historical window, and inspecting the same data invalidate casual significance/profitability claims. Historical RSI votes used >60 bullish / <40 bearish, not 70/30 fades. MACD votes used histogram sign, not necessarily crossover events.

`zero_dte/bb_watcher.py` currently configures only 5m and checks the close against a band, not the high/low wick. Its class documentation is partly outdated. `core/indicators.py` already supplies BB, RSI, and MACD math. Existing research in `docs/research/intraday-momentum-2026-09-04.md` does not establish a robust generic continuation/reversal edge and does not test this exact proposal.

## Small experiment to identify actual incremental impact

1. Freeze BB(20,2), RSI(14), MACD(12,26,9) as initial conventions, not optimized settings.
2. Build all three timeframes from the same SPX minute source. Require complete, session-aligned candles. Define regular-session history, overnight handling, and warm-up explicitly: 20 fifteen-minute bars alone span five hours; MACD needs additional stabilization history.
3. Define contact against either the previous completed band or the newly completed candle's band; keep those separate. Use only information actually available at alert time. Intrabar wick contact cannot be reconstructed with precise live timing from completed minute OHLC.
4. Record band side, wick/close/re-entry state, %b, normalized BandWidth and its change, middle-band slope, RSI level/change, MACD histogram sign/change, and completion time for every timeframe. Keep higher-timeframe values frozen until their candles close.
5. Compare BB-only events against BB + RSI, BB + MACD, BB + volatility/trend context, and BB + higher-timeframe context. Limit combinations; adding every filter at once prevents attribution and invites overfitting.
6. Predeclare a primary horizon; retain other fixed forward horizons as secondary. Measure signed SPX movement, favorable/adverse excursion, event frequency, and latency. Compare improvements on matched events and report coverage lost to filtering, not just a higher selected hit rate.
7. Split chronologically into development and untouched holdout, avoid overlapping outcome windows across boundaries, and evaluate uncertainty by session rather than treating every minute as independent. Include quiet/trending/choppy sessions and scheduled-event periods without proliferating tiny subgroups.
8. Paper-log the frozen rule before promoting notifications. Preserve existing frozen Action/ORB versions. Any options-profitability study needs actual contract selection, executable bid/ask, spread, fees, latency, and expiry handling; SPX points are not option P&L.

An alert should describe observed states, e.g. "1m upper-band contact; 5m closed back inside; RSI falling; 15m momentum still positive." Mark this as mixed/rejection-watch, not a fabricated confidence percentage or automatic puts recommendation. Deduplicate by event and re-arm only after a defined reset.

## Third-party shortlist

| Tool | Potential use | Recommendation and limits |
|---|---|---|
| TradingView / Pine + webhooks | Independent chart comparison, deterministic multi-timeframe indicators and alert delivery | Best first external complement; not inherently AI. Confirm real-time SPX entitlement, alert plan limits and bar-close behavior. Chart/feed/session settings must match. |
| TrendSpider | Visual research, Strategy Tester, Sidekick AI assistance, ML model experimentation | Most relevant AI-platform trial for this workflow. Homepage currently advertises **ML Quant Lab** and Sidekick. Verify SPX coverage, 1m historical depth, multi-timeframe backtest semantics, export/webhook access and plan costs before buying. |
| QuantConnect / LEAN | Reproducible Python research and more realistic portfolio/options testing | Research infrastructure, not a purchased winning signal. Dataset entitlements and exact SPX/SPXW history/execution modeling require separate checking. Greater migration/engineering cost than testing in the existing repo. |
| OpenAI API | Explain computed event snapshots, assist with research code, summarize evaluated results | Suitable optional explanation layer, not the indicator calculator, market-data source or autonomous order authority. Structured tool inputs can constrain facts but do not guarantee correctness. |
| Trade Ideas / Holly | Candidate commercial AI-signal service | Lower priority for this SPX-specific problem. Current product capabilities and integration were not established: the retrieved `/holly/` URL resolved to a 2016 post. Verify current underlying coverage, APIs/export and independently auditable outcomes before considering. |

### Verified integration details and gaps

- [TradingView webhooks](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/): requires 2FA; receiver must respond within three seconds; delivery can fail. Use a validated, rate-limited receiver, fast acknowledgement, queued processing and event IDs/deduplication. Do not embed credentials in payloads. A receiver reachable from TradingView adds operational/security work for the Pi.
- [TrendSpider homepage](https://trendspider.com/): retrieved successfully and advertises Strategy Tester, Sidekick, and ML Quant Lab. Dedicated `/platform/strategy-tester/` and `/platform/ai-strategy-lab/` pages returned HTTP 403, so their detailed capabilities were not verified. Do not assume an unrestricted bot API.
- [OpenAI function calling](https://platform.openai.com/docs/guides/function-calling): documents a mechanism for an application to supply computed facts through tools. The bot should calculate numeric values deterministically, validate outputs, and work without AI when requests fail. Do not send licensed market data to any outside AI provider without checking redistribution/processing terms, retention and privacy.
- [QuantConnect research guidance](https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide) was retrieved; no dataset purchase or live platform test was performed.
- No current prices, subscriptions, paid market-data requests, API integrations, vendor performance, or live delivery were validated. Vendor claims do not demonstrate trading edge.

## Recommended decision

Do not buy an AI signal feed or replace production with another untested indicator score. First run a small BB rejection/continuation event study using existing data infrastructure, with TradingView as an optional chart cross-check. Trial TrendSpider only if visual research saves enough work and its specific SPX/timeframe/data requirements check out. Add an LLM later only if interpreting validated events, rather than generating signals, is the actual bottleneck.
