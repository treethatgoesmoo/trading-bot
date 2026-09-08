# Retired BB-event notification stream

The raw `bb-events-v1` stream is being replaced by the owner-requested bullish/bearish/volatile market-state service. Its cogs and embeds have been removed; there is no legacy notification mode. See the [current README](../README.md) and [product scope](product-scope.md) for integration status and rules.

Original BB ledgers remain untouched migration sources. Do not relabel their deliveries or outcomes as new state alerts. The earlier BB behavior/deployment guide remains in Git history, including revision `338036e`.
