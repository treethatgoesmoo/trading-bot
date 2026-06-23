# Robinhood Gold Level II — Feasibility & Integration Research

**Status:** research only (no code yet). Decision: use **derived signal only** — the bot
computes an order-book signal privately and posts only that, never the raw Nasdaq depth.

---

## 1. Verdict

**Feasible.** A Robinhood **Gold** subscription unlocks Nasdaq TotalView Level II depth, and
the unofficial `robin_stocks` Python library exposes it via a documented function pair:

```python
robin_stocks.robinhood.stocks.get_pricebook_by_symbol(symbol, info=None)  # -> {"asks": [...], "bids": [...]}
robin_stocks.robinhood.stocks.get_pricebook_by_id(stock_id, info=None)
```

Both are documented as *"Represents Level II Market Data provided for Gold subscribers."*
`get_pricebook_by_symbol` just resolves the symbol to an instrument id and calls the id form.

This is the **only** realistic path: Robinhood has **no official public stock data API**
(its only official APIs are Crypto trading and the Legend desktop product). `robin_stocks`
calls Robinhood's private app endpoints.

---

## 2. How it works

### Endpoint
Under the hood the call hits Robinhood's private market-data endpoint:

```
GET https://api.robinhood.com/marketdata/pricebook/snapshots/{instrument_id}/
```

- Requires an **authenticated** session (Gold account).
- Returns a **point-in-time snapshot** — this is NOT a streaming/websocket feed. To track
  the book over time you **poll** it.
- Depth is **Nasdaq TotalView only** — no NYSE/other venues, no Robinhood internal flow.

### Response shape (to confirm empirically at prototype time)
A dictionary with `asks` and `bids` arrays, each level roughly:

```jsonc
{
  "asks": [
    { "side": "ask", "price": { "amount": "501.2400", "currency_code": "USD" }, "quantity": 300 },
    ...
  ],
  "bids": [
    { "side": "bid", "price": { "amount": "501.1800", "currency_code": "USD" }, "quantity": 850 },
    ...
  ],
  "updated_at": "2026-06-..."
}
```

Each level carries a **price** (`price.amount`) and an **aggregate quantity**. The exact
sub-fields (and how many levels are returned) must be confirmed by running it once — the
library docs don't publish the schema.

### Authentication & session persistence
```python
import pyotp
import robin_stocks.robinhood as r

totp = pyotp.TOTP(TOTP_SECRET).now()          # TOTP_SECRET from RH 2FA setup ("other" app)
r.login(username, password, mfa_code=totp)     # prompts interactively if mfa_code omitted
```

- MFA is mandatory in practice. The clean automated path: enable 2FA in the RH app, choose
  **"other / can't scan"**, and store the **alphanumeric TOTP secret** Robinhood shows. Feed
  it to `pyotp` to generate the 6-digit code at login. (Keep the backup code somewhere safe —
  losing the secret can lock you out.)
- `robin_stocks` **persists the session by default** (a pickle token under the user's home,
  e.g. `~/.tokens/robinhood.pickle`, ~24h expiry) so a long-running bot logs in once and
  reuses the token until it expires, then re-logs. *(Exact path/expiry to verify at prototype.)*

---

## 3. Derived-signal design (what the bot will actually post)

The raw Nasdaq book **stays inside the module**. We post only computed signals:

1. **Order-book imbalance** — over the top *N* levels:
   `imbalance = bid_volume / (bid_volume + ask_volume)`
   - `> 0.60` → buy-side pressure (bullish)
   - `< 0.40` → sell-side pressure (bearish)
   - else neutral
2. **Liquidity walls near key levels** — large resting size clustered just above the
   first-candle high (resistance) or just below the low (support). Flag size ≥ k× the
   average level size.
3. **Spread / total depth** — context (thin book = less reliable).

Output is a small dataclass, e.g.:
```python
@dataclass
class L2Signal:
    direction: str        # BULLISH | BEARISH | NEUTRAL
    imbalance: float      # 0..1
    bid_depth: int
    ask_depth: int
    walls: list[str]      # e.g. ["ask wall 2k @ 502.00 (resistance)"]
```

### Integration points
- New module `robinhood_l2.py` (auth + snapshot fetch + signal computation). `robin_stocks`
  is **synchronous**, so wrap calls in `asyncio.to_thread(...)` to keep the bot's async loop
  responsive.
- Add **`L2_Imbalance`** as a new weighted indicator inside `compute_30m_forecast`, and/or
  show a derived line in the forecast embed (e.g. *"Order book: bid-heavy 64% · ask wall @ 502"*).
- Config (`.env`): `ROBINHOOD_USERNAME`, `ROBINHOOD_PASSWORD`, `ROBINHOOD_TOTP_SECRET`,
  `L2_ENABLED` (default off), `L2_DEPTH_LEVELS` (default ~10), `L2_TICKERS`.
- Poll **modestly** — align with the 30-min forecast cadence, or only sample around decision
  points. Hammering the private endpoint risks rate-limiting / account flags.

---

## 4. Risks & mitigations

| Risk | Detail | Mitigation |
|---|---|---|
| **Nasdaq data licensing** | TotalView is licensed for **personal, in-app display**. Re-posting raw depth to Discord = redistribution → violates the data agreement / RH ToS. | **Derived-signal only.** Never emit raw price+size ladders. (Chosen.) |
| **Unofficial API / ToS** | `robin_stocks` uses private endpoints; programmatic access is a gray area and can break or get an account flagged. | Keep it optional/off by default; poll modestly; accept it may break. |
| **Credential security** | Bot needs username/password + TOTP secret persisted. | Store in `.env` (already git-ignored); never log them; rely on the cached session token so creds are used rarely. |
| **Snapshot, not stream** | Polled snapshots can miss fast book changes. | Treat L2 as a confirming/context signal, not a trigger. |
| **Nasdaq-only depth** | Misses other venues. | Document the limitation; don't over-trust thin books. |
| **Account risk** | This is your real brokerage login. | Read-only usage (no order endpoints touched); consider whether you're comfortable automating against the account at all. |

---

## 5. Open questions to resolve at prototype time
1. Exact JSON schema and **how many depth levels** the snapshot returns.
2. Real session-pickle path/expiry and re-login behavior in the installed `robin_stocks` version.
3. Practical rate limits before flags/throttling.
4. Does L2 imbalance actually improve forecast accuracy? → backtest is hard (no historical L2),
   so validate **forward** using the new dashboard's per-indicator accuracy on `L2_Imbalance`.

---

## 6. Sources
- [Robinhood — What's Level II Market Data?](https://robinhood.com/us/en/support/articles/level-ii-market-data/)
- [Robinhood Newsroom — Nasdaq Level 2 Market Data is Here](https://robinhood.com/us/en/newsroom/nasdaq-level-2-market-data-is-here/)
- [robin_stocks docs — Robinhood functions (pricebook)](https://robin-stocks.readthedocs.io/en/latest/robinhood.html)
- [robin_stocks source — stocks.get_pricebook_by_symbol / by_id](https://robin-stocks.readthedocs.io/en/latest/_modules/robin_stocks/robinhood/stocks.html)
- [robin_stocks — Quick Start (login / MFA)](https://robin-stocks.readthedocs.io/en/latest/quickstart.html)
- [Nasdaq TotalView](https://www.nasdaq.com/solutions/data/equities/nasdaq-totalview)
