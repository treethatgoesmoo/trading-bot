"""0DTE SPX option premium range capture.

Picks one same-day-expiry ATM call and one ATM put on the SPX chain shortly
after the open, samples their premiums through the session, and at the close
reports each side's *effective* range:

  effective max = highest premium observed (quote samples, widened by the
                  contract's own daily bar when Yahoo serves one)
  effective min = lowest premium observed, floored DOWN to the settlement
                  value max(0, intrinsic at the closing print) — a 0DTE
                  option that finishes OTM is worth 0 even though the last
                  quoted low never prints it.

Everything here is synchronous (yfinance is blocking) — the cog calls it via
asyncio.to_thread(...). Failures always degrade: pick returns None (the loop
retries next poll), a failed sample is skipped (the next one is minutes away),
and finalize falls back to the sampled extremes per side rather than raising.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime

import pytz
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class TrackedSide:
    contract_symbol: str
    kind: str             # "call" | "put"
    strike: float
    expiry: str           # YYYY-MM-DD (same day — 0DTE)
    open_mid: float       # first quote captured at pick time
    obs_high: float       # highest premium seen so far
    obs_low: float        # lowest premium seen so far
    last_mid: float
    samples: int = 1
    quote_stale: bool = False
    # Set by finalize_day():
    settlement: float | None = None      # max(0, intrinsic at the close)
    effective_min: float | None = None   # min(observed, settlement)
    effective_max: float | None = None   # max(observed, settlement)


@dataclass
class SpxOptionDay:
    date: str             # YYYY-MM-DD
    ticker: str           # underlying, e.g. ^SPX
    spot_at_pick: float
    call: TrackedSide
    put: TrackedSide
    spot_close: float | None = None
    finalized: bool = False


def _num(val, default=0.0) -> float:
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _quote(row) -> tuple[float, bool]:
    """Premium estimate for a chain row. A zero bid is real on a near-worthless
    0DTE contract, so the mid only needs a live ask; with the ask gone too
    (frozen outside regular hours) fall back to the last trade, flagged stale."""
    bid, ask, last = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("lastPrice"))
    if ask > 0:
        return round((bid + ask) / 2, 2), False
    return round(last, 2), True


def _spot(tk) -> float | None:
    try:
        price = tk.fast_info["lastPrice"]
        if price:
            return float(price)
    except Exception:
        pass
    try:
        h = tk.history(period="1d", interval="1m")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def pick_day_contracts(ticker: str, today: date) -> SpxOptionDay | None:
    """Pick the ATM call + put expiring today. None (retry later) when the
    chain, a same-day expiry, the spot, or a usable ATM quote isn't there yet."""
    today_iso = today.strftime("%Y-%m-%d")
    try:
        tk = yf.Ticker(ticker)
        expiries = list(tk.options or [])
        if today_iso not in expiries:
            logger.warning("[%s] No same-day (0DTE) expiry %s (have: %s)",
                           ticker, today_iso, expiries[:4])
            return None
        spot = _spot(tk)
        if spot is None:
            logger.warning("[%s] No spot price for 0DTE contract pick", ticker)
            return None
        chain = tk.option_chain(today_iso)
    except Exception as exc:
        logger.warning("[%s] 0DTE chain lookup failed: %s", ticker, exc)
        return None

    sides = {}
    for kind, rows in (("call", chain.calls), ("put", chain.puts)):
        if rows is None or rows.empty:
            logger.warning("[%s] Empty 0DTE %s chain for %s", ticker, kind, today_iso)
            return None
        row = rows.iloc[(rows["strike"] - spot).abs().argsort().iloc[0]]
        mid, stale = _quote(row)
        if mid <= 0:
            logger.warning("[%s] ATM 0DTE %s has no usable quote yet", ticker, kind)
            return None
        sides[kind] = TrackedSide(
            contract_symbol=str(row.get("contractSymbol", "")),
            kind=kind,
            strike=round(_num(row.get("strike")), 2),
            expiry=today_iso,
            open_mid=mid,
            obs_high=mid,
            obs_low=mid,
            last_mid=mid,
            quote_stale=stale,
        )
    return SpxOptionDay(date=today_iso, ticker=ticker, spot_at_pick=round(spot, 2),
                        call=sides["call"], put=sides["put"])


def sample_day(day: SpxOptionDay) -> bool:
    """Requote both tracked contracts once and update the running extremes.
    Returns True when at least one side recorded a quote."""
    try:
        chain = yf.Ticker(day.ticker).option_chain(day.call.expiry)
    except Exception as exc:
        logger.warning("[%s] 0DTE sample failed: %s", day.ticker, exc)
        return False
    updated = False
    for side, rows in ((day.call, chain.calls), (day.put, chain.puts)):
        if rows is None or rows.empty:
            continue
        match = rows[(rows["strike"] - side.strike).abs() < 0.01]
        if match.empty:
            continue
        mid, stale = _quote(match.iloc[0])
        if mid <= 0 and stale:
            continue  # no live quote and no last trade — nothing to record
        side.last_mid = mid
        side.quote_stale = stale
        side.samples += 1
        side.obs_high = max(side.obs_high, mid)
        side.obs_low = min(side.obs_low, mid)
        updated = True
    return updated


def fetch_close_spot(ticker: str) -> float | None:
    """Closing print of the underlying — the 0DTE cash-settlement reference."""
    try:
        h = yf.Ticker(ticker).history(period="1d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("[%s] Close fetch failed: %s", ticker, exc)
    return None


def finalize_day(day: SpxOptionDay, spot_close: float | None) -> None:
    """Compute each side's effective min/max. Degrades per side: the contract's
    own daily bar (true traded extremes between samples) is best-effort, and a
    missing close leaves the settlement floor off rather than aborting."""
    day.spot_close = round(spot_close, 2) if spot_close is not None else None
    for side in (day.call, day.put):
        try:
            bars = yf.Ticker(side.contract_symbol).history(period="1d")
            if not bars.empty:
                hi, lo = _num(bars["High"].iloc[-1]), _num(bars["Low"].iloc[-1])
                if hi > 0:
                    side.obs_high = max(side.obs_high, round(hi, 2))
                if lo > 0:
                    side.obs_low = min(side.obs_low, round(lo, 2))
        except Exception as exc:
            logger.info("[%s] No day bar for %s (%s) — using sampled extremes",
                        day.ticker, side.contract_symbol, exc)
        if day.spot_close is not None:
            intrinsic = (day.spot_close - side.strike if side.kind == "call"
                         else side.strike - day.spot_close)
            side.settlement = round(max(0.0, intrinsic), 2)
            side.effective_min = round(min(side.obs_low, side.settlement), 2)
            side.effective_max = round(max(side.obs_high, side.settlement), 2)
        else:
            side.settlement = None
            side.effective_min = side.obs_low
            side.effective_max = side.obs_high
    day.finalized = True


# ── Persistence rows (data/spx_options-YYYY-MM-DD.jsonl) ─────────────────

def _utcnow_iso() -> str:
    return datetime.now(pytz.UTC).isoformat()


def pick_entry(day: SpxOptionDay) -> dict:
    return {"type": "pick", "time": _utcnow_iso(), "date": day.date,
            "ticker": day.ticker, "spot": day.spot_at_pick,
            "call": asdict(day.call), "put": asdict(day.put)}


def sample_entry(day: SpxOptionDay) -> dict:
    return {"type": "sample", "time": _utcnow_iso(),
            "call_mid": day.call.last_mid, "call_stale": day.call.quote_stale,
            "put_mid": day.put.last_mid, "put_stale": day.put.quote_stale}


def final_entry(day: SpxOptionDay) -> dict:
    return {"type": "final", "time": _utcnow_iso(), "date": day.date,
            "ticker": day.ticker, "spot": day.spot_at_pick,
            "spot_close": day.spot_close,
            "call": asdict(day.call), "put": asdict(day.put)}


def rehydrate(entries: list[dict]) -> SpxOptionDay | None:
    """Rebuild today's tracking state after a restart from the day's rows.
    The pick row recreates the contracts, sample rows replay the extremes,
    and a final row (if the restart happened after close) ends the day."""
    day = None
    for e in entries:
        etype = e.get("type")
        if etype == "pick":
            try:
                day = SpxOptionDay(date=e["date"], ticker=e["ticker"],
                                   spot_at_pick=e["spot"],
                                   call=TrackedSide(**e["call"]),
                                   put=TrackedSide(**e["put"]))
            except (KeyError, TypeError) as exc:
                logger.warning("Corrupt spx_options pick row — starting fresh: %s", exc)
                return None
        elif etype == "sample" and day is not None:
            for side, prefix in ((day.call, "call"), (day.put, "put")):
                mid = e.get(f"{prefix}_mid")
                if isinstance(mid, (int, float)) and mid > 0:
                    side.last_mid = mid
                    side.samples += 1
                    side.obs_high = max(side.obs_high, mid)
                    side.obs_low = min(side.obs_low, mid)
        elif etype == "final" and day is not None:
            try:
                day.call = TrackedSide(**e["call"])
                day.put = TrackedSide(**e["put"])
                day.spot_close = e.get("spot_close")
                day.finalized = True
            except (KeyError, TypeError) as exc:
                logger.warning("Corrupt spx_options final row ignored: %s", exc)
    return day
