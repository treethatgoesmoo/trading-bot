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

The initial "what could this be worth" calculation follows the Opening Range
Breakout (ORB) playbook: the first SPX_OPTIONS_ORB_MINUTES (default 15) of
the session set the opening range, and the classic measured-move targets —
range high + mult x range height above, range low - mult x range height
below — price each side's projected max as intrinsic at its breakout target
(fetch_opening_range / apply_orb_projection).

Everything here is synchronous (yfinance is blocking) — the cog calls it via
asyncio.to_thread(...). Failures always degrade: pick returns None (the loop
retries next poll), a failed sample is skipped (the next one is minutes
away), and finalize falls back to the sampled extremes per side rather than
raising.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

import pytz
import yfinance as yf

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


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
    # Set by apply_orb_projection() once the opening range is in:
    projected_max: float | None = None   # intrinsic at the ORB breakout target
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
    # Opening range (first SPX_OPTIONS_ORB_MINUTES of the session) and the
    # ORB measured-move breakout targets (apply_orb_projection):
    or_high: float | None = None
    or_low: float | None = None
    proj_high: float | None = None       # or_high + mult x range height
    proj_low: float | None = None        # or_low  - mult x range height


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


def fetch_opening_range(ticker: str, today: date, minutes: int) -> tuple[float, float] | None:
    """High/low of the session's first `minutes` (the ORB opening range) from
    5-min bars. None until every bar of the window is in — the range isn't
    set before the window closes, and a partial fetch must not fake one."""
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m")
    except Exception as exc:
        logger.warning("[%s] Opening range fetch failed: %s", ticker, exc)
        return None
    if h is None or h.empty:
        return None
    idx = h.index
    try:
        idx = idx.tz_convert(ET)
    except TypeError:  # tz-naive index — assume exchange time
        idx = idx.tz_localize(ET)
    start = ET.localize(datetime.combine(today, time(9, 30)))
    window = h[(idx >= start) & (idx < start + timedelta(minutes=minutes))]
    if len(window) < max(1, minutes // 5):
        return None
    return float(window["High"].max()), float(window["Low"].min())


def apply_orb_projection(day: SpxOptionDay, or_high: float, or_low: float,
                         target_mult: float = 1.0) -> None:
    """Derive each side's projected max from the ORB measured move: breakout
    targets sit `target_mult` x the opening-range height beyond the range.

    Projected max = intrinsic at the side's breakout target, floored at the
    opening mid (with no breakout the premium just decays from there).
    Conservative: ignores time value left when the target prints early. No
    projected min is derived — the downside of an ATM 0DTE side is expiring
    worthless, which the *effective* min captures at the close.
    """
    if or_high <= or_low:
        return
    height = or_high - or_low
    day.or_high = round(or_high, 2)
    day.or_low = round(or_low, 2)
    day.proj_high = round(or_high + target_mult * height, 2)
    day.proj_low = round(or_low - target_mult * height, 2)
    for side in (day.call, day.put):
        intrinsic = (day.proj_high - side.strike if side.kind == "call"
                     else side.strike - day.proj_low)
        side.projected_max = round(max(side.open_mid, intrinsic), 2)


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
        hi = lo = 0.0
        try:
            bars = yf.Ticker(side.contract_symbol).history(period="1d")
            if not bars.empty:
                hi, lo = _num(bars["High"].iloc[-1]), _num(bars["Low"].iloc[-1])
        except Exception as exc:
            logger.info("[%s] No day bar for %s (%s) — using sampled extremes",
                        day.ticker, side.contract_symbol, exc)
        if hi > 0:
            side.obs_high = max(side.obs_high, round(hi, 2))
        if lo > 0:
            side.obs_low = min(side.obs_low, round(lo, 2))
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


def projection_entry(day: SpxOptionDay) -> dict:
    return {"type": "projection", "time": _utcnow_iso(),
            "or_high": day.or_high, "or_low": day.or_low,
            "proj_high": day.proj_high, "proj_low": day.proj_low,
            "call_projected_max": day.call.projected_max,
            "put_projected_max": day.put.projected_max}


def sample_entry(day: SpxOptionDay) -> dict:
    return {"type": "sample", "time": _utcnow_iso(),
            "call_mid": day.call.last_mid, "call_stale": day.call.quote_stale,
            "put_mid": day.put.last_mid, "put_stale": day.put.quote_stale}


def final_entry(day: SpxOptionDay) -> dict:
    return {"type": "final", "time": _utcnow_iso(), "date": day.date,
            "ticker": day.ticker, "spot": day.spot_at_pick,
            "spot_close": day.spot_close,
            "or_high": day.or_high, "or_low": day.or_low,
            "proj_high": day.proj_high, "proj_low": day.proj_low,
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
        elif etype == "projection" and day is not None:
            day.or_high = e.get("or_high")
            day.or_low = e.get("or_low")
            day.proj_high = e.get("proj_high")
            day.proj_low = e.get("proj_low")
            for side, prefix in ((day.call, "call"), (day.put, "put")):
                side.projected_max = e.get(f"{prefix}_projected_max")
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


# ── Live self-test ───────────────────────────────────────────────────────
# `python -m zero_dte.spx_options` from the repo root exercises the real
# yfinance paths with the .env config: spot, opening range, the ATM pick,
# and one premium sample. Read-only — it never writes to data/ or touches
# the bot's state.

def _self_test() -> int:
    import sys

    from config import SPX_OPTIONS_ORB_MINUTES, SPX_OPTIONS_TICKER

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else SPX_OPTIONS_TICKER
    today = datetime.now(ET).date()
    print(f"Self-test for {ticker} on {today} (ET)")

    print("[1/3] Spot via yfinance...")
    spot = _spot(yf.Ticker(ticker))
    print(f"      {'OK: $%.2f' % spot if spot else 'FAIL: no spot (market data unavailable?)'}")

    print(f"[2/3] Opening range (first {SPX_OPTIONS_ORB_MINUTES}m) via yfinance...")
    orb = fetch_opening_range(ticker, today, SPX_OPTIONS_ORB_MINUTES)
    if orb:
        print(f"      OK: ${orb[1]:,.2f} – ${orb[0]:,.2f}")
    else:
        print("      not available (normal before ~9:45 ET, on non-trading days, "
              "or when yfinance has no 5m bars yet)")

    print("[3/3] ATM 0DTE pick + one premium sample...")
    day = pick_day_contracts(ticker, today)
    if day is None:
        print("      no pick (normal outside trading days — needs a same-day expiry "
              "and a quotable ATM chain)")
        return 0
    if orb:
        apply_orb_projection(day, orb[0], orb[1])
    sample_day(day)
    for side in (day.call, day.put):
        proj = f" projected_max=${side.projected_max:,.2f}" if side.projected_max else ""
        print(f"      {side.kind.upper()} {side.strike:g} {side.contract_symbol}: "
              f"open=${side.open_mid:,.2f} last=${side.last_mid:,.2f} "
              f"range=${side.obs_low:,.2f}–${side.obs_high:,.2f}"
              f"{' (stale quote)' if side.quote_stale else ''}{proj}")
    print("Done — all live paths reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
