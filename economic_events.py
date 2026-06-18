"""Kalshi economic-event calendar.

Pulls upcoming *major macro events* (FOMC decision, CPI, jobs report, GDP,
Core PCE) from Kalshi's public market-data API and derives the market-implied
outcome for each. These scheduled releases routinely move the broad market
(e.g. an FOMC decision), so the bot posts a morning briefing of what's coming
up and how Kalshi is currently pricing it.

No API key/account required — Kalshi's read endpoints are public.

Outcome logic depends on the event's market structure:
  * mutually_exclusive events (FOMC: Cut / Maintain / Hike) → report the single
    most-likely bucket (highest implied probability) plus the runner-up.
  * threshold ladders (CPI/jobs/GDP/PCE: "Above X") → report the implied median,
    i.e. the threshold the market prices closest to a coin flip.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
import pytz

import config as cfg

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")


@dataclass(frozen=True)
class SeriesSpec:
    """A Kalshi series we treat as a market-moving macro release."""
    ticker: str
    name: str
    emoji: str


# Curated "major movers" — display order is the order shown in the briefing.
# KXFEDDECISION is mutually-exclusive (Cut/Maintain/Hike) so it yields a clean
# direction; the rest are "Above X" threshold ladders.
TRACKED_SERIES: list[SeriesSpec] = [
    SeriesSpec("KXFEDDECISION", "FOMC Rate Decision", "\U0001f3e6"),  # 🏦
    SeriesSpec("KXCPIYOY", "CPI Inflation (YoY)", "\U0001f4c8"),       # 📈
    SeriesSpec("KXPAYROLLS", "Jobs Report (Payrolls)", "\U0001f4bc"),  # 💼
    SeriesSpec("KXGDP", "GDP Growth", "\U0001f3ed"),                   # 🏭
    SeriesSpec("KXPCECORE", "Core PCE Inflation", "\U0001f4b5"),       # 💵
]


@dataclass
class Outcome:
    label: str
    prob: float  # 0.0 - 1.0


@dataclass
class EconEvent:
    series_ticker: str
    name: str
    emoji: str
    event_ticker: str
    release_dt: datetime          # in US/Eastern
    is_today: bool
    mutually_exclusive: bool
    consensus: str                # human-readable headline
    top_outcomes: list[Outcome] = field(default_factory=list)
    url: str = ""


# ── Kalshi parsing helpers ───────────────────────────────────────────────

def _market_prob(m: dict) -> float:
    """Implied YES probability (0-1) for a Kalshi market.

    Prefers the bid/ask midpoint (live two-sided quote); falls back to last
    traded price, then a one-sided quote. Prices are dollar strings in [0, 1].
    """
    def _f(key: str) -> float:
        try:
            return float(m.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    bid, ask, last = _f("yes_bid_dollars"), _f("yes_ask_dollars"), _f("last_price_dollars")
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if last > 0:
        return last
    return ask or bid


def _parse_outcome(ev_json: dict) -> tuple[bool, str, list[Outcome]]:
    """Return (mutually_exclusive, consensus_text, top_outcomes)."""
    markets = ev_json.get("markets") or []
    me = bool(ev_json.get("mutually_exclusive"))

    scored = []
    for m in markets:
        label = (m.get("yes_sub_title") or m.get("subtitle") or "Yes").strip()
        prob = _market_prob(m)
        if prob > 0:
            scored.append(Outcome(label=label, prob=prob))

    if not scored:
        return me, "No market data yet", []

    if me:
        # Single most-likely bucket wins; keep up to 3 for context.
        scored.sort(key=lambda o: o.prob, reverse=True)
        top = scored[:3]
        consensus = f"{top[0].label} — {top[0].prob:.0%}"
        return me, consensus, top

    # Threshold ladder: the market closest to 50% is the implied median.
    median = min(scored, key=lambda o: abs(o.prob - 0.5))
    consensus = f"Implied median near “{median.label}” ({median.prob:.0%})"
    return me, consensus, [median]


def _release_dt(market: dict) -> datetime | None:
    """Best-estimate release/settlement time of a market, in US/Eastern.

    Prefers ``occurrence_datetime`` (the scheduled data release), falling back
    to ``expected_expiration_time`` (when Kalshi expects to settle).
    """
    raw = market.get("occurrence_datetime") or market.get("expected_expiration_time")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(ET)


# ── Fetching ─────────────────────────────────────────────────────────────

async def _get_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict | None:
    """GET with light retry on 429 (Kalshi rate-limits bursts)."""
    for attempt in range(3):
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if resp.status != 200:
                logger.warning("Kalshi GET %s: HTTP %s", url, resp.status)
                return None
            return await resp.json()
    logger.warning("Kalshi GET %s: rate limited after retries", url)
    return None


async def _fetch_series_event(
    session: aiohttp.ClientSession,
    spec: SeriesSpec,
    start_of_today: datetime,
    horizon: datetime,
    now: datetime,
) -> EconEvent | None:
    """Return the nearest upcoming event for a series within the window, or None."""
    url = f"{cfg.KALSHI_API_BASE}/events"
    params = {
        "series_ticker": spec.ticker,
        "status": "open",
        "with_nested_markets": "true",
        "limit": "50",
    }
    data = await _get_json(session, url, params)
    if data is None:
        return None

    candidates: list[tuple[datetime, dict]] = []
    for ev in data.get("events") or []:
        markets = ev.get("markets") or []
        if not markets:
            continue
        dt = _release_dt(markets[0])
        if dt is None:
            continue
        if start_of_today <= dt <= horizon:
            candidates.append((dt, ev))

    if not candidates:
        return None

    release_dt, ev_json = min(candidates, key=lambda c: c[0])
    me, consensus, top = _parse_outcome(ev_json)
    event_ticker = ev_json.get("event_ticker", spec.ticker)
    return EconEvent(
        series_ticker=spec.ticker,
        name=spec.name,
        emoji=spec.emoji,
        event_ticker=event_ticker,
        release_dt=release_dt,
        is_today=release_dt.date() == now.date(),
        mutually_exclusive=me,
        consensus=consensus,
        top_outcomes=top,
        url=f"https://kalshi.com/markets/{spec.ticker.lower()}",
    )


async def fetch_events(lookahead_days: int | None = None, now: datetime | None = None) -> list[EconEvent]:
    """Fetch upcoming major macro events from Kalshi, sorted by release time.

    Args:
        lookahead_days: How far ahead to look (default: cfg.ECON_LOOKAHEAD_DAYS).
        now: Override "current time" (US/Eastern) — for testing.

    Returns events with release time between the start of today and the horizon.
    Best-effort: any series that errors is skipped, not raised.
    """
    if lookahead_days is None:
        lookahead_days = cfg.ECON_LOOKAHEAD_DAYS
    now = now or datetime.now(ET)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = now + timedelta(days=lookahead_days)

    timeout = aiohttp.ClientTimeout(total=15)
    events: list[EconEvent] = []
    # Sequential, not concurrent: Kalshi 429s on bursts and this runs once a day.
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for spec in TRACKED_SERIES:
            try:
                ev = await _fetch_series_event(session, spec, start_of_today, horizon, now)
                if ev is not None:
                    events.append(ev)
            except Exception as exc:  # best-effort: one bad series shouldn't sink the briefing
                logger.warning("Kalshi fetch failed for %s: %s", spec.ticker, exc)

    events.sort(key=lambda e: e.release_dt)
    return events


if __name__ == "__main__":
    # Quick manual check: python economic_events.py [lookahead_days]
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console emoji
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45

    async def _main():
        evs = await fetch_events(lookahead_days=days)
        if not evs:
            print(f"No major economic events in the next {days} days.")
            return
        for e in evs:
            tag = " [TODAY]" if e.is_today else ""
            print(f"{e.emoji} {e.name}{tag}")
            print(f"   {e.release_dt.strftime('%a %b %d, %Y %I:%M %p ET')}  ({e.event_ticker})")
            print(f"   {e.consensus}")
            for o in e.top_outcomes[1:3]:
                print(f"      - {o.label} {o.prob:.0%}")

    asyncio.run(_main())
