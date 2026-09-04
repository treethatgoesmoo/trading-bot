# Intraday momentum research snapshot — 2026-09-04

This file preserves provenance for the results recorded in `docs/strategy-log.md` without committing the full Yahoo intraday snapshots. Raw CSVs and sidecar metadata are stored locally under ignored `.research-data/`.

## Hypotheses

1. **Generic next-bar continuation:** Does a larger absolute three-interval move, normalized by signal-time ATR(14), increase continuation over the exact next 5m/15m bar?
2. **Extension/reversal:** Does RSI/VWAP ATR-normalized extension increase exact-next-bar reversal?
3. **Literature-aligned market intraday momentum:** Does previous-close-to-10:00 ET return predict the final half-hour return?

Labels never cross sessions, and all four momentum-lookback closes must come from the same session. Generic tests begin when enough same-session history exists and use signal-time ATR. Score ties stay in the same quantile bucket. The opening/closing test uses one observation per complete trading day and compares opening volume/range only with trailing sessions.

## Primary sources

- Gao, Han, Li, and Zhou, “Market Intraday Momentum,” *Journal of Financial Economics* (2018): https://doi.org/10.1016/j.jfineco.2018.05.009
- Baltussen, Da, Lammers, and Martens, “Hedging Demand and Market Intraday Momentum,” *Journal of Financial Economics* (2021): https://doi.org/10.1016/j.jfineco.2021.04.029
- Heston, Korajczyk, and Sadka, “Intraday Patterns in the Cross-section of Stock Returns,” *Journal of Finance* (2010): https://doi.org/10.1111/j.1540-6261.2010.01573.x

The literature supports a specific opening-to-closing relationship and conditional effects, not generic all-session next-bar continuation.

## Commands

```bash
python backtest_momentum_extension.py --ticker SPY --interval 5 --refresh
python backtest_momentum_extension.py --ticker SPY --interval 15 --refresh
python backtest_momentum_extension.py --ticker QQQ --interval 15 --refresh
python backtest_momentum_extension.py --ticker IWM --interval 15 --refresh
python backtest_momentum_extension.py --ticker DIA --interval 15 --refresh
python backtest_open_close_momentum.py --ticker SPY
python backtest_timeframe_context.py
python analyze_session_thirds.py --stream 5
python analyze_session_thirds.py --stream 15
```

Environment: Python package `yfinance==1.2.2`, Yahoo 60-day intraday history, `auto_adjust=False`.

## Input identity

| Ticker | Interval | UTC range | Rows | CSV SHA-256 |
|---|---:|---|---:|---|
| SPY | 5m | 2026-06-11 13:30 — 2026-09-04 18:00 | 4,657 | `ccd4ecc8d21beb4fd539b4a0daf28eaebf720b6e0bdf1cff3605b333764b44f6` |
| SPY | 15m | 2026-06-11 13:30 — 2026-09-04 18:00 | 1,553 | `9f7d682ce19ecae29765798e356568f793d0f005ad6e5eb576b29b0593cdd9d7` |
| QQQ | 15m | 2026-06-11 13:30 — 2026-09-04 18:00 | 1,553 | `302477f03c1cdf835f79c8dd555a26ab1e9146110b84b2fa6dbdf336d13c4d6b` |
| IWM | 15m | 2026-06-11 13:30 — 2026-09-04 18:00 | 1,553 | `9a03e5b3302ee92f4a876648563fae7fd6f6a3c6cb91e21da31f34da287b9d8a` |
| DIA | 15m | 2026-06-11 13:30 — 2026-09-04 18:00 | 1,553 | `eee1283de34bc9bbac72a2da24966e8073a72d8142198dc1a52122da3c6ee7c9` |

The hashes identify the exact evidence but do not make ignored raw data independently retrievable. Preserve the `.research-data/` artifact externally if exact third-party reproduction is required.

## Results and decision

- SPY 5m, high absolute-momentum bucket: 50.8% continuation conditional on a directional move, versus 52.5% low bucket.
- SPY 15m: 51.4% high versus 47.9% low; the larger unconditional continuation rate mostly reflects fewer FLAT outcomes.
- 15m high-bucket conditional continuation failed cross-asset robustness: QQQ 48.1%, IWM 44.6%, DIA 46.2%.
- Extension/reversal results were non-monotonic and inconsistent.
- Opening-to-closing SPY test: 58 days, correlation +0.061, bootstrap 95% interval −0.253 to +0.281, directional agreement 41.4%.
- Independent accumulated live logs through 2026-09-03 also weakened rather than strengthened the rolling forecast case. On exact paired directional rows, 15m forecast/baseline accuracy was 54.5%/54.5% at the open (n=33), 54.2%/51.2% midday (n=203), and 44.4%/52.4% near the close (n=63). The muted 5m stream was 62.3%/56.5% (n=69), 50.1%/50.3% (n=483), and 47.1%/55.6% (n=187), respectively. These repository logs use legacy grading schema 1 and are reported separately from schema 2.

### Higher-timeframe display follow-up

Completed 30m and 1h candles were also tested as predictors of the exact next directional 15m outcome. The 30m accuracy was SPY 48.3%, QQQ 47.1%, IWM 46.9%, and DIA 47.5%; 1h was 50.3%, 49.4%, 49.0%, and 49.0% (n=774–816). None cleared the same-row always-BULLISH baseline robustly. They may be shown as backward-looking context but must not enter the forecast vote without new evidence.

**Decision:** keep momentum/extension scores and completed higher-timeframe candles descriptive-only. Do not add them to the weighted forecast or post a new alert. The literature-aligned hypothesis remains research-only until a multi-year frozen dataset and untouched chronological holdout are available.
