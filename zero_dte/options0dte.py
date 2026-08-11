"""0DTE option projection — quote the ATM same-day call/put and project each
contract's price range from the day forecast's projected high/low.

Flow (run once per day when the morning day forecast posts):
  1. Pick today's expiry from the yfinance chain (SPY/QQQ have daily expiries;
     tickers without one skip gracefully).
  2. Quote the ATM call and put (strike closest to spot).
  3. Calibrate a volatility for each contract: implied vol solved from the
     observed mid (so the projection reprices the contract the market is
     actually showing), falling back to the chain's IV column.
  4. Black-Scholes reprice at underlying = projected high and projected low,
     holding time-to-expiry at capture (theta not modeled — noted on the
     embed) plus an intrinsic-only floor variant (value if the level is hit
     at the close). Both variants are logged so the tuning loop can learn
     which assumption tracks reality.

Grading (EOD): each contract has its own Yahoo ticker whose daily OHLC bar is
the actual traded range — compare projection vs actual and append to
data/option_outcomes-*.jsonl.

yfinance is synchronous — every fetch goes through asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime

import pytz
import yfinance as yf

from config import OPTION_RISK_FREE_RATE
from core.options_math import bs_price, implied_vol

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

_EXPIRY_HOUR_ET = 16  # 0DTE options settle on the 4pm close


@dataclass
class OptionProjection:
    """One ATM contract quoted at capture, with its projected price range."""
    ticker: str
    contract_symbol: str
    kind: str                 # "call" | "put"
    strike: float
    expiry: str               # YYYY-MM-DD (today)
    spot: float               # underlying at capture
    premium: float            # option mid (or last) at capture
    bid: float
    ask: float
    implied_vol: float | None     # vol used for repricing
    iv_source: str            # "mid" | "chain" | "none"
    exp_high: float           # underlying projected high (from day forecast)
    exp_low: float            # underlying projected low
    proj_high: float          # option value at the favorable projected level
    proj_low: float           # option value at the adverse projected level
    proj_high_intrinsic: float    # same levels valued at expiry (theta fully paid)
    proj_low_intrinsic: float
    t_years: float            # time to expiry at capture (years)
    quote_stale: bool
    timestamp: str            # UTC ISO capture time


def _num(val, default=0.0) -> float:
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _last_price(ticker: str) -> float | None:
    """Blocking: latest 1-min close (regular hours). Runs in a thread."""
    df = yf.Ticker(ticker).history(period="1d", interval="1m")
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _quote_atm(ticker: str, spot: float, expiry: str) -> dict | None:
    """Blocking: ATM call + put rows for one expiry. Runs in a thread."""
    chain = yf.Ticker(ticker).option_chain(expiry)
    out = {}
    for kind, rows in (("call", chain.calls), ("put", chain.puts)):
        if rows is None or rows.empty:
            return None
        idx = (rows["strike"] - spot).abs().idxmin()
        row = rows.loc[idx]
        bid, ask, last = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("lastPrice"))
        mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else round(last, 2)
        out[kind] = {
            "contract_symbol": str(row.get("contractSymbol", "")),
            "strike": _num(row.get("strike")),
            "bid": bid, "ask": ask, "last": last, "mid": mid,
            "implied_vol": _num(row.get("impliedVolatility")) or None,
            "quote_stale": not (bid > 0 and ask > 0),
        }
    return out


async def build_projections(ticker: str, spot: float, exp_high: float,
                            exp_low: float) -> list[OptionProjection]:
    """ATM call + put projections for today's expiry; [] when the ticker has
    no same-day expiry or quoting fails (never raises). `spot` is a fallback
    (the day's open) — the live price at capture is fetched below."""
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")
    try:
        expiries = await asyncio.to_thread(lambda: yf.Ticker(ticker).options)
    except Exception as exc:
        logger.warning("[%s] Option expiries fetch failed: %s", ticker, exc)
        return []
    if not expiries or today not in expiries:
        logger.info("[%s] No 0DTE expiry today — option projection skipped", ticker)
        return []

    # Spot at CAPTURE, not the 9:30 open the day forecast anchors to. The
    # capture runs minutes after the open; if price has drifted, an open-price
    # spot picks the wrong ATM strike and poisons the IV calibration — a mid
    # embedding real intrinsic solved against a spot where intrinsic is zero
    # inflates sigma, or falls outside the model's reachable band and silently
    # drops to stale chain IV.
    try:
        live = await asyncio.to_thread(_last_price, ticker)
    except Exception:
        live = None
    if live:
        spot = live

    try:
        quotes = await asyncio.to_thread(_quote_atm, ticker, spot, today)
    except Exception as exc:
        logger.warning("[%s] ATM quote failed: %s", ticker, exc)
        return []
    if not quotes:
        return []

    expiry_dt = now_et.replace(hour=_EXPIRY_HOUR_ET, minute=0, second=0, microsecond=0)
    t_years = max((expiry_dt - now_et).total_seconds(), 0.0) / (365.0 * 24 * 3600)
    ts = datetime.now(pytz.UTC).isoformat()

    projections = []
    for kind, q in quotes.items():
        if q["mid"] <= 0:
            logger.info("[%s] ATM %s has no usable quote — skipped", ticker, kind)
            continue
        # Calibrate vol from the observed mid so repricing at spot returns the
        # market's own price; chain IV is the fallback (yfinance IV is often
        # stale or zero outside market hours).
        sigma = implied_vol(q["mid"], spot, q["strike"], t_years,
                            OPTION_RISK_FREE_RATE, kind)
        iv_source = "mid"
        if sigma is None:
            sigma, iv_source = q["implied_vol"], "chain"
        if not sigma:
            sigma, iv_source = None, "none"

        # Favorable level for a call is the projected high; for a put the low.
        fav, adv = (exp_high, exp_low) if kind == "call" else (exp_low, exp_high)
        if sigma is not None:
            proj_high = bs_price(fav, q["strike"], t_years, OPTION_RISK_FREE_RATE, sigma, kind)
            proj_low = bs_price(adv, q["strike"], t_years, OPTION_RISK_FREE_RATE, sigma, kind)
        else:
            proj_high = bs_price(fav, q["strike"], 0.0, OPTION_RISK_FREE_RATE, 0.0, kind)
            proj_low = bs_price(adv, q["strike"], 0.0, OPTION_RISK_FREE_RATE, 0.0, kind)

        projections.append(OptionProjection(
            ticker=ticker,
            contract_symbol=q["contract_symbol"],
            kind=kind,
            strike=q["strike"],
            expiry=today,
            spot=round(spot, 2),
            premium=q["mid"],
            bid=q["bid"], ask=q["ask"],
            implied_vol=round(sigma, 4) if sigma else None,
            iv_source=iv_source,
            exp_high=exp_high, exp_low=exp_low,
            proj_high=round(proj_high, 2),
            proj_low=round(proj_low, 2),
            proj_high_intrinsic=round(bs_price(fav, q["strike"], 0.0, 0.0, 0.0, kind), 2),
            proj_low_intrinsic=round(bs_price(adv, q["strike"], 0.0, 0.0, 0.0, kind), 2),
            t_years=round(t_years, 6),
            quote_stale=q["quote_stale"],
            timestamp=ts,
        ))
    return projections


def projection_dict(proj: OptionProjection) -> dict:
    return asdict(proj)


async def fetch_contract_ohlc(contract_symbol: str,
                              expiry: str | None = None) -> dict | None:
    """Actual daily OHLC bar for an option contract (its own Yahoo ticker).
    Used by EOD grading, which may run a day or two late after downtime — so
    fetch a few days and pick the bar dated `expiry` (a 0DTE contract's only
    real session) rather than blindly taking the latest row. Returns None
    when Yahoo has no matching bar (no trades / not yet published)."""
    def _dl():
        df = yf.Ticker(contract_symbol).history(period="5d")
        if df is None or df.empty:
            return None
        if expiry:
            match = df[df.index.strftime("%Y-%m-%d") == expiry]
            if match.empty:
                return None
            row = match.iloc[-1]
        else:
            row = df.iloc[-1]
        return {
            "high": round(_num(row.get("High")), 2),
            "low": round(_num(row.get("Low")), 2),
            "close": round(_num(row.get("Close")), 2),
            "volume": int(_num(row.get("Volume"))),
        }
    try:
        return await asyncio.to_thread(_dl)
    except Exception as exc:
        logger.warning("Contract OHLC fetch failed for %s: %s", contract_symbol, exc)
        return None
