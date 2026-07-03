"""Options chain lookup and contract selection via yfinance.

Everything here is synchronous (yfinance is blocking) — callers in the bot
wrap these in asyncio.to_thread(...). Failures always degrade: pick_contract
returns None (signal still posts without a contract) and bad quotes are
flagged (quote_stale / illiquid), never raised out of a scan.

Known yfinance quirks handled: empty .options list, NaN/zero bid-ask outside
regular hours (quotes frozen at prior close), and holiday weeks with no expiry
inside the primary 7-14 DTE window.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime

import yfinance as yf

from config import (
    SWING_DTE_FALLBACK_MAX,
    SWING_DTE_MAX,
    SWING_DTE_MIN,
    SWING_MAX_SPREAD_PCT,
    SWING_MIN_OPEN_INTEREST,
    SWING_STRIKE_OTM_MAX_PCT,
)

logger = logging.getLogger(__name__)

DTE_SWEET_SPOT = 10  # aim for ~10 calendar days to expiry


@dataclass
class OptionContract:
    contract_symbol: str
    expiry: str           # YYYY-MM-DD
    dte: int
    kind: str             # "call" | "put"
    strike: float
    bid: float
    ask: float
    mid: float            # entry premium estimate
    last: float
    open_interest: int
    volume: int
    implied_vol: float | None
    spread_pct: float
    breakeven: float
    illiquid: bool = False
    quote_stale: bool = False


def _num(val, default=0.0) -> float:
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _pick_expiry(expiries: list[str], today: date) -> tuple[str, int] | None:
    """Expiry closest to the sweet spot within [DTE_MIN, DTE_MAX]; widen to
    DTE_FALLBACK_MAX when a holiday week leaves the primary window empty."""
    dted = []
    for e in expiries:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        dted.append((e, (d - today).days))

    for max_dte in (SWING_DTE_MAX, SWING_DTE_FALLBACK_MAX):
        window = [(e, dte) for e, dte in dted if SWING_DTE_MIN <= dte <= max_dte]
        if window:
            return min(window, key=lambda x: abs(x[1] - DTE_SWEET_SPOT))
    return None


def pick_contract(ticker: str, direction: str, spot: float,
                  today: date | None = None) -> OptionContract | None:
    """Pick a 7-14 DTE contract near the money for a CALL/PUT signal.

    Strike: nearest-to-spot within [ATM, ATM + SWING_STRIKE_OTM_MAX_PCT% OTM]
    that has decent open interest; falls back to nearest-to-spot flagged
    illiquid. Returns None only when no chain/expiry is available at all.
    """
    today = today or datetime.now().date()
    kind = "call" if direction.upper() == "CALL" else "put"
    try:
        tk = yf.Ticker(ticker)
        expiries = list(tk.options or [])
        if not expiries:
            logger.warning("[%s] No option expiries from yfinance", ticker)
            return None
        picked = _pick_expiry(expiries, today)
        if picked is None:
            logger.warning("[%s] No expiry within %d-%d DTE (have: %s)",
                           ticker, SWING_DTE_MIN, SWING_DTE_FALLBACK_MAX, expiries[:6])
            return None
        expiry, dte = picked

        chain = tk.option_chain(expiry)
        rows = chain.calls if kind == "call" else chain.puts
        if rows is None or rows.empty:
            logger.warning("[%s] Empty %s chain for %s", ticker, kind, expiry)
            return None
    except Exception as exc:
        logger.warning("[%s] Option chain lookup failed: %s", ticker, exc)
        return None

    # Candidate strikes: ATM to slightly OTM in the trade direction.
    otm_limit = spot * SWING_STRIKE_OTM_MAX_PCT / 100.0
    if kind == "call":
        cands = rows[(rows["strike"] >= spot - 0.01) & (rows["strike"] <= spot + otm_limit)]
    else:
        cands = rows[(rows["strike"] <= spot + 0.01) & (rows["strike"] >= spot - otm_limit)]
    if cands.empty:
        cands = rows  # fall back to whole chain, nearest-to-spot below

    illiquid = False
    liquid = cands[cands["openInterest"].fillna(0) >= SWING_MIN_OPEN_INTEREST]
    if liquid.empty:
        liquid = cands
        illiquid = True
    row = liquid.iloc[(liquid["strike"] - spot).abs().argsort().iloc[0]]

    strike = _num(row.get("strike"))
    bid = _num(row.get("bid"))
    ask = _num(row.get("ask"))
    last = _num(row.get("lastPrice"))
    quote_stale = False
    if bid > 0 and ask > 0:
        mid = round((bid + ask) / 2, 2)
        spread_pct = round((ask - bid) / mid * 100, 1) if mid else 0.0
    else:
        # Outside regular hours yfinance zeroes bid/ask — quote from last trade.
        mid = round(last, 2)
        spread_pct = 0.0
        quote_stale = True
    if mid <= 0:
        logger.warning("[%s] %s %s %s has no usable quote", ticker, expiry, strike, kind)
        return None
    if spread_pct > SWING_MAX_SPREAD_PCT:
        illiquid = True

    breakeven = strike + mid if kind == "call" else strike - mid
    iv = _num(row.get("impliedVolatility"), default=float("nan"))

    return OptionContract(
        contract_symbol=str(row.get("contractSymbol", "")),
        expiry=expiry,
        dte=dte,
        kind=kind,
        strike=round(strike, 2),
        bid=round(bid, 2),
        ask=round(ask, 2),
        mid=mid,
        last=round(last, 2),
        open_interest=int(_num(row.get("openInterest"))),
        volume=int(_num(row.get("volume"))),
        implied_vol=round(iv, 4) if not math.isnan(iv) else None,
        spread_pct=spread_pct,
        breakeven=round(breakeven, 2),
        illiquid=illiquid,
        quote_stale=quote_stale,
    )


def requote(ticker: str, expiry: str, strike: float, kind: str) -> float | None:
    """Current mid (or last) for an exact contract — used when grading a signal
    before its expiry. Returns None if the contract can no longer be quoted."""
    try:
        chain = yf.Ticker(ticker).option_chain(expiry)
        rows = chain.calls if kind == "call" else chain.puts
        match = rows[(rows["strike"] - strike).abs() < 0.01]
        if match.empty:
            return None
        row = match.iloc[0]
        bid, ask, last = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("lastPrice"))
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        return round(last, 2) if last > 0 else None
    except Exception as exc:
        logger.warning("[%s] Requote failed for %s %s %s: %s", ticker, expiry, strike, kind, exc)
        return None
