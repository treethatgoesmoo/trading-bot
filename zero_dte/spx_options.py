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

Option premiums prefer the Massive (formerly Polygon.io) REST API when
MASSIVE_API_KEY is set, using free-tier endpoints only — the reference
contracts list and per-contract aggregates (snapshot endpoints return
NOT_AUTHORIZED on the free plan). Each side's session bars carry the traded
high/low so far, so extremes between samples are captured; calls are
throttled to the free tier's 5 calls/min, and data is 15-min delayed (the
day-bar catch-up at finalize covers the tail). Index bars (spot, opening
range, settlement close) and any Massive failure use yfinance.

Everything here is synchronous (yfinance/requests are blocking) — the cog
calls it via asyncio.to_thread(...). Failures always degrade: pick returns
None (the loop retries next poll), a failed sample is skipped (the next one
is minutes away), and finalize falls back to the sampled extremes per side
rather than raising.
"""

from __future__ import annotations

import logging
import math
import threading
import time as time_mod
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

import pytz
import requests
import yfinance as yf

from config import MASSIVE_API_KEY, MASSIVE_CALLS_PER_MIN

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


# ── Massive (formerly Polygon.io) REST client ────────────────────────────
# Primary source for option premiums when MASSIVE_API_KEY is set. Free-tier
# endpoints only: /v3/reference/options/contracts + /v2/aggs per contract
# (snapshots are NOT_AUTHORIZED on the free plan). Contract tickers are OCC
# symbols with an "O:" prefix (O:SPXW260806C06400000), and an index
# underlying maps ^SPX -> I:SPX. Index bars (spot / opening range /
# settlement close) intentionally stay on yfinance — index data is a separate
# Massive entitlement, and any Massive failure here falls back to yfinance.

MASSIVE_BASE = "https://api.massive.com"
_MASSIVE_WINDOW_SECONDS = 60.0
_massive_lock = threading.Lock()
_massive_calls: deque[float] = deque()


def _massive_throttle() -> None:
    """Block until a call slot is free in the sliding 1-minute window. Holding
    the lock through the sleep deliberately serializes concurrent callers."""
    with _massive_lock:
        now = time_mod.monotonic()
        while _massive_calls and now - _massive_calls[0] > _MASSIVE_WINDOW_SECONDS:
            _massive_calls.popleft()
        if len(_massive_calls) >= MASSIVE_CALLS_PER_MIN:
            time_mod.sleep(_MASSIVE_WINDOW_SECONDS - (now - _massive_calls[0]) + 0.1)
            now = time_mod.monotonic()
            while _massive_calls and now - _massive_calls[0] > _MASSIVE_WINDOW_SECONDS:
                _massive_calls.popleft()
        _massive_calls.append(time_mod.monotonic())


def _massive_get(path: str, params: dict | None = None) -> dict | None:
    """Rate-limited GET returning parsed JSON, or None on any failure so the
    caller falls back to yfinance."""
    if not MASSIVE_API_KEY:
        return None
    _massive_throttle()
    try:
        resp = requests.get(f"{MASSIVE_BASE}{path}",
                            params={**(params or {}), "apiKey": MASSIVE_API_KEY},
                            timeout=15)
        if resp.status_code != 200:
            logger.warning("Massive %s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("Massive %s failed: %s", path, exc)
        return None


def _massive_underlying(ticker: str) -> str:
    return "I:" + ticker[1:] if ticker.startswith("^") else ticker


def _massive_contracts(ticker: str, expiry_iso: str, strike_lo: float,
                       strike_hi: float) -> list[dict] | None:
    """Contracts near the money from the reference endpoint (free tier —
    snapshots are NOT_AUTHORIZED there). One call covers both types."""
    data = _massive_get("/v3/reference/options/contracts", {
        "underlying_ticker": _massive_underlying(ticker),
        "expiration_date": expiry_iso,
        "strike_price.gte": strike_lo,
        "strike_price.lte": strike_hi,
        "limit": 250,
    })
    if data is None:
        return None
    return data.get("results") or []


def _massive_session_bars(contract_symbol: str, date_iso: str) -> dict | None:
    """Today's 5-min bars for a contract, reduced to {last, high, low}. Trades
    only (no NBBO on the free tier), 15-min delayed — but the bars carry the
    session's full high/low so far, which is exactly what range capture needs.
    None on API failure or when the contract hasn't traded yet."""
    data = _massive_get(
        f"/v2/aggs/ticker/O:{contract_symbol}/range/5/minute/{date_iso}/{date_iso}",
        {"limit": 500, "sort": "asc"})
    results = (data or {}).get("results") or []
    closes = [_num(b.get("c")) for b in results]
    highs = [h for b in results if (h := _num(b.get("h"))) > 0]
    lows = [l for b in results if (l := _num(b.get("l"))) > 0]
    if not closes or not highs or not lows:
        return None
    return {"last": round(closes[-1], 2), "first": round(_num(results[0].get("o")) or closes[0], 2),
            "high": round(max(highs), 2), "low": round(min(lows), 2)}


def _apply_massive_bars(side: TrackedSide, bars: dict) -> bool:
    """Fold a session-bar summary into a side: extremes from the bar high/low,
    last close as the current premium. Bar data is trades, not a frozen quote."""
    side.obs_high = max(side.obs_high, bars["high"])
    side.obs_low = min(side.obs_low, bars["low"])
    if bars["last"] > 0:
        side.last_mid = bars["last"]
        side.quote_stale = False
        side.samples += 1
    return True


def _pick_via_massive(ticker: str, today: date, spot: float) -> SpxOptionDay | None:
    """ATM pick from the reference contracts list + each contract's session
    bars (3 calls total). The nearest strike per type is priced off its first
    traded bar; an ATM 0DTE that hasn't traded by pick time isn't ready yet."""
    today_iso = today.strftime("%Y-%m-%d")
    rows = _massive_contracts(ticker, today_iso, round(spot * 0.995), round(spot * 1.005))
    if not rows:
        if rows is not None:
            logger.warning("[%s] Massive lists no 0DTE contracts near $%.2f", ticker, spot)
        return None
    sides = {}
    for kind in ("call", "put"):
        cands = [r for r in rows if r.get("contract_type") == kind]
        if not cands:
            logger.warning("[%s] Massive 0DTE chain has no %s side near $%.2f", ticker, kind, spot)
            return None
        row = min(cands, key=lambda r: abs(_num(r.get("strike_price")) - spot))
        symbol = str(row.get("ticker", "")).removeprefix("O:")
        bars = _massive_session_bars(symbol, today_iso)
        if bars is None:
            logger.warning("[%s] Massive 0DTE %s %s has no trades yet", ticker, kind, symbol)
            return None
        side = TrackedSide(
            contract_symbol=symbol,
            kind=kind,
            strike=round(_num(row.get("strike_price")), 2),
            expiry=today_iso,
            open_mid=bars["first"],
            obs_high=bars["high"],
            obs_low=bars["low"],
            last_mid=bars["last"],
            quote_stale=False,
        )
        sides[kind] = side
    return SpxOptionDay(date=today_iso, ticker=ticker, spot_at_pick=round(spot, 2),
                        call=sides["call"], put=sides["put"])


def _sample_via_massive(day: SpxOptionDay) -> bool | None:
    """Refresh both sides from their session bars (2 calls). Full-day bars
    each time make sampling self-healing — a missed tick loses nothing. None
    means Massive produced nothing (caller falls back to yfinance)."""
    updated = False
    for side in (day.call, day.put):
        bars = _massive_session_bars(side.contract_symbol, day.date)
        if bars is not None:
            updated = _apply_massive_bars(side, bars) or updated
    return True if updated else None


def _massive_day_bar(contract_symbol: str, date_iso: str) -> tuple[float, float] | None:
    """The contract's daily high/low from Massive aggregates."""
    data = _massive_get(f"/v2/aggs/ticker/O:{contract_symbol}/range/1/day/{date_iso}/{date_iso}")
    results = (data or {}).get("results") or []
    if not results:
        return None
    return _num(results[0].get("h")), _num(results[0].get("l"))


# ── Pick / sample / finalize (Massive first, yfinance fallback) ──────────

def pick_day_contracts(ticker: str, today: date) -> SpxOptionDay | None:
    """Pick the ATM call + put expiring today. None (retry later) when the
    chain, a same-day expiry, the spot, or a usable ATM quote isn't there yet."""
    if MASSIVE_API_KEY:
        spot = _spot(yf.Ticker(ticker))
        if spot is not None:
            day = _pick_via_massive(ticker, today, spot)
            if day is not None:
                return day
        logger.info("[%s] Massive pick unavailable — falling back to yfinance", ticker)
    return _pick_via_yfinance(ticker, today)


def _pick_via_yfinance(ticker: str, today: date) -> SpxOptionDay | None:
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
    if MASSIVE_API_KEY:
        result = _sample_via_massive(day)
        if result is not None:
            return result
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
        bar = _massive_day_bar(side.contract_symbol, day.date) if MASSIVE_API_KEY else None
        if bar is not None:
            hi, lo = bar
        else:
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
# APIs with the .env credentials: Massive connectivity/entitlement, spot,
# opening range, the ATM pick, and one premium sample. Read-only — it never
# writes to data/ or touches the bot's state.

def _self_test() -> int:
    import sys

    from config import SPX_OPTIONS_ORB_MINUTES, SPX_OPTIONS_TICKER

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ticker = sys.argv[1].upper() if len(sys.argv) > 1 else SPX_OPTIONS_TICKER
    today = datetime.now(ET).date()
    print(f"Self-test for {ticker} on {today} (ET)")

    if MASSIVE_API_KEY:
        print(f"[1/4] Massive key set ({MASSIVE_API_KEY[:4]}...) — checking the contracts list...")
        data = _massive_get("/v3/reference/options/contracts",
                            {"underlying_ticker": _massive_underlying(ticker), "limit": 5})
        if data is None:
            print("      FAIL: no response (see the warning above — bad key, no "
                  "entitlement for this underlying, or no network). "
                  "yfinance fallback would be used live.")
        else:
            results = data.get("results") or []
            print(f"      OK: status={data.get('status')} contracts_returned={len(results)}")
            if results:
                r = results[0]
                print(f"      sample contract: {r.get('ticker')} strike={r.get('strike_price')} "
                      f"exp={r.get('expiration_date')} type={r.get('contract_type')}")
                bars = _massive_session_bars(str(r.get("ticker", "")).removeprefix("O:"),
                                             today.strftime("%Y-%m-%d"))
                print(f"      session bars today: {bars if bars else 'none (normal outside trading hours)'}")
    else:
        print("[1/4] MASSIVE_API_KEY not set — yfinance only")

    print("[2/4] Spot via yfinance...")
    spot = _spot(yf.Ticker(ticker))
    print(f"      {'OK: $%.2f' % spot if spot else 'FAIL: no spot (market data unavailable?)'}")

    print(f"[3/4] Opening range (first {SPX_OPTIONS_ORB_MINUTES}m) via yfinance...")
    orb = fetch_opening_range(ticker, today, SPX_OPTIONS_ORB_MINUTES)
    if orb:
        print(f"      OK: ${orb[1]:,.2f} – ${orb[0]:,.2f}")
    else:
        print("      not available (normal before ~9:45 ET, on non-trading days, "
              "or when yfinance has no 5m bars yet)")

    print("[4/4] ATM 0DTE pick + one premium sample...")
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
