"""On-demand historical SPY range estimate; no live evidence/delivery writes.

Twenty preceding regular sessions (including calendar early closes) supply
separate 80th-percentile open-to-low and open-to-high excursions. This is NOT
an 80% joint coverage band, a validated forecast, or support/resistance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math

import discord

from core import market_calendar
from core.session_bars import new_record, session_time, update_record, utc, validate_record
from core.thetadata_client import ThetaDataClient
from spy_setup.embeds import display_time

VERSION = 'spy-range-v1'
SAMPLE_SIZE = 20
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class RangeResult:
    embed: discord.Embed
    history: dict
    anchor: float
    candle_close: datetime


def _sessions(day):
    result = []
    candidate = day - timedelta(days=1)
    while len(result) < SAMPLE_SIZE:
        if market_calendar.is_trading_day(candidate):
            result.append(candidate)
        candidate -= timedelta(days=1)
    return result


def _close(day):
    return session_time(day, *market_calendar.close_time(day))


def _validate(record, day, now, *, received_by, complete):
    validate_record(record, day)
    bars = record['bars']
    if not bars or record['rejected_bars'] or record['fetch_failures']:
        raise ValueError('Missing or rejected SPY bars')
    opening = session_time(day, 9, 30)
    for index, bar in enumerate(bars):
        if (utc(bar['timestamp']) != opening + index * _MINUTE
                or utc(bar['available_at']) > received_by
                or utc(bar['timestamp']) + _MINUTE > now):
            raise ValueError('Noncontiguous or future SPY evidence')
    last_close = utc(bars[-1]['timestamp']) + _MINUTE
    if complete:
        if last_close != _close(day):
            raise ValueError('Incomplete historical or closed SPY session')
    elif not 0 <= (now - last_close).total_seconds() <= 90:
        raise ValueError('Stale SPY current-session evidence')
    return bars


def _quantile(values):
    """Deterministic linearly interpolated empirical 80th percentile."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * .8
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _fetch(client, day, cutoff, clock, *, current=False):
    rows = client.stock_history_ohlc(
        day, symbol='SPY', interval='1m', start_time='09:30:00',
        end_time=cutoff.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
    received_at = clock()
    if current and cutoff < _close(day):
        # The provider may include the requested end boundary. It is an
        # unfinished candle, not evidence; never let its price affect the band.
        rows = [row for row in rows if utc(row['timestamp'] if isinstance(row, dict)
                                          else row.timestamp) != cutoff]
    record = new_record(day)
    update_record(record, rows, received_at, data_cutoff=cutoff)
    return record, received_at


def range_session(day, now, *, history=None, anchor=None, client_factory=None, clock=None):
    """Return a RangeResult using an isolated client.

    First call makes at most 21 sequential requests; a supplied history cache
    must match the exact twenty preceding dates and is revalidated, not mutated.
    Subsequent calls still fetch today's completed minutes once. No retries,
    live ledger writes, or configuration/opt-in changes occur here. The caller
    owns private-channel access, request opt-in, cooldown and cache lifetime.
    ``now`` is a fixed as-of cutoff, not advanced during provider requests.
    ``clock`` supplies actual receipt times; it defaults to the UTC wall clock.
    A caller-retained ``anchor`` freezes the initially accepted opening price
    for its process/session; this module does not persist forecasts. The caller
    must check ``candle_close`` freshness at delivery time, not only request time.
    """
    now = utc(now)
    if (type(day) is not date or day != now.astimezone(market_calendar.ET).date()
            or not market_calendar.is_trading_day(day)
            or now < session_time(day, 9, 31)):
        raise ValueError('Range requires today\'s trading session after its first completed minute')
    if anchor is not None:
        if isinstance(anchor, bool) or not isinstance(anchor, (int, float)) or not math.isfinite(anchor) or anchor <= 0:
            raise ValueError('Range anchor must be finite and positive')
        anchor = float(anchor)
    receipt_clock = clock or (lambda: datetime.now(timezone.utc))
    last_receipt = now

    def observed_time():
        nonlocal last_receipt
        observed = utc(receipt_clock())
        if observed < last_receipt:
            raise ValueError('Receipt clock precedes cutoff or moved backwards')
        last_receipt = observed
        return observed

    received_by = observed_time()
    sessions = _sessions(day)
    if history is not None:
        if not isinstance(history, dict) or set(history) != set(sessions):
            raise ValueError('Range history must contain exactly the twenty preceding sessions')
        for session in sessions:
            _validate(history[session], session, now, received_by=received_by, complete=True)

    client = (client_factory or ThetaDataClient)(purpose='unspecified')
    try:
        if history is None:
            history = {}
            for session in sessions:
                record, received_at = _fetch(client, session, _close(session), observed_time)
                _validate(record, session, now, received_by=received_at, complete=True)
                history[session] = record
        cutoff = min(now.replace(second=0, microsecond=0), _close(day))
        current, received_at = _fetch(client, day, cutoff, observed_time, current=True)
        bars = _validate(current, day, now, received_by=received_at, complete=now >= _close(day))
    finally:
        client.close()

    downside, upside = [], []
    for session in sessions:
        prior = history[session]['bars']
        prior_open = prior[0]['open']
        downside.append((prior_open - min(bar['low'] for bar in prior)) / prior_open)
        upside.append((max(bar['high'] for bar in prior) - prior_open) / prior_open)
    anchor = float(bars[0]['open']) if anchor is None else anchor
    candle_close = utc(bars[-1]['timestamp']) + _MINUTE
    lower, upper = anchor * (1 - _quantile(downside)), anchor * (1 + _quantile(upside))
    actual_low, actual_high = min(bar['low'] for bar in bars), max(bar['high'] for bar in bars)
    breached = []
    if actual_low < lower:
        breached.append('below estimated low')
    if actual_high > upper:
        breached.append('above estimated high')
    embed = discord.Embed(
        title=f'SPY projected daily range | {day}', color=0x3498DB, timestamp=candle_close,
        description=f'**Estimated SPY low–high: ${lower:.2f}–${upper:.2f}**\n'
                    'Historical estimate, not guaranteed support/resistance or an entry signal.')
    embed.add_field(name='Initial accepted open anchor', value=f'SPY ${anchor:.2f} • today\'s 09:30 ET open reference\n'
                    'Frozen when retained by the caller for this process/session; not a persisted forecast. '
                    'Historical revisions or a restart can change a newly calculated estimate.', inline=False)
    embed.add_field(name='Observed session', value=f'SPY low–high: ${actual_low:.2f}–${actual_high:.2f}\n'
                    f'Latest completed close: ${bars[-1]["close"]:.2f}\n'
                    f'Candle close: {display_time(utc(bars[-1]["timestamp"]) + _MINUTE, include_date=True)}', inline=False)
    embed.add_field(name='Range check', value=('Observed breach: ' + ' and '.join(breached)) if breached
                    else 'Observed prices remain inside the estimate.', inline=False)
    embed.add_field(name='Method and sample', value=f'{SAMPLE_SIZE} preceding regular sessions: '
                    f'{min(sessions)} through {max(sessions)} (includes early closes).\n'
                    'Separate 80th-percentile percentage moves from each session open to its low/high, '
                    'applied to today\'s open. Fixed anchor; not recentered on latest price. '
                    'Not an 80% coverage guarantee; accuracy is unvalidated.', inline=False)
    embed.add_field(name='Data as of', value=f'{display_time(now, include_date=True)}'
                    + (' • Session closed; observed full-day range.' if now >= _close(day) else '')
                    + '\nTheta Nasdaq Basic (nqb); historical data may be revised.', inline=False)
    embed.set_footer(text=f'{VERSION} | SPY prices only; no SPX option prices or returns inferred.')
    return RangeResult(embed=embed, history=history, anchor=anchor, candle_close=candle_close)
