"""Completed-candle Bollinger events, not price forecasts or trade instructions.

BB(20, 2, population std), RSI(14), MACD(12, 26, 9) use shared math.
MIN_HISTORY=35 is a fixed warm-up convention, NOT proof of EMA convergence.
Up to two complete immediately preceding regular sessions are prepended as an
unbroken chain. Overnight returns are included, but candles never span sessions.
Two normal prior sessions warm 15m for its first completed current candle; short
sessions can still leave insufficient history. No history is fetched here.

A contact compares the completed candle's high/low to its own completed bands.
This is an end-of-candle observation, NOT a claim of intrabar band contact time.
Each side re-arms after a candle no longer touches that side. A rejection is a
fresh wick contact closing inside, or an outside close returning inside. An
outside-close event occurs on entry; continuation only on the SECOND consecutive
outside close. The third and later outside closes do not repeatedly notify.
"""
from __future__ import annotations

from datetime import date, timedelta
import math

import pandas as pd

from core import market_calendar
from core.indicators import bollinger, macd, rsi_series
from zero_dte.orb_audit import session_time, utc, valid_bar

VERSION = 'bb-events-v1'
TIMEFRAMES = ('1', '5', '15')
MIN_HISTORY = 35
_MINUTE = timedelta(minutes=1)


def preceding_sessions(day):
    """The two immediately preceding trading dates, newest first."""
    days = []
    while len(days) < 2:
        day -= timedelta(days=1)
        if market_calendar.is_trading_day(day):
            days.append(day)
    return days


def _empty(reason):
    return dict(available=False, reason=reason, bar_at=None, close=None,
                upper=None, middle=None, lower=None, rsi=None, rsi_change=None,
                macd_histogram=None, macd_change=None, band_width=None,
                state='unavailable', events=[])


def _rows(bars, day, now):
    """Validate available rows; ignore unfinished/future observations causally.

    Missing receipt times are invalid: persisted ORB collector rows have them.
    Duplicates/corrupt eligible observations invalidate the sequence, not merely
    one candle. Input ordering is immaterial.
    """
    rows = {}
    for bar in bars:
        stamp = utc(bar['timestamp'])
        available = utc(bar['available_at'])
        if stamp + _MINUTE > now or available > now:
            continue
        if available < stamp + _MINUTE or not valid_bar(bar, day, now) or stamp in rows:
            raise ValueError('Invalid or duplicate minute')
        rows[stamp] = {'timestamp': stamp, **{k: float(bar[k]) for k in ('open', 'high', 'low', 'close')}}
    return [rows[t] for t in sorted(rows)]


def _contiguous(rows, opening, end):
    count = int((end - opening).total_seconds() / 60)
    return len(rows) == count and all(row['timestamp'] == opening + i * _MINUTE for i, row in enumerate(rows))


def _buckets(rows, day, minutes):
    """Only full session-open-aligned buckets; never bridge an overnight gap."""
    opening = session_time(day, 9, 30)
    grouped = {}
    for row in rows:
        index = int((row['timestamp'] - opening).total_seconds() // (minutes * 60))
        grouped.setdefault(index, []).append(row)
    result = []
    for index in sorted(grouped):
        group = grouped[index]
        start = opening + timedelta(minutes=index * minutes)
        if not _contiguous(group, start, start + timedelta(minutes=minutes)):
            continue
        result.append(dict(timestamp=start, open=group[0]['open'],
                           high=max(r['high'] for r in group), low=min(r['low'] for r in group),
                           close=group[-1]['close']))
    return result


def _finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


def _frame(rows):
    closes = pd.Series([r['close'] for r in rows], dtype=float)
    upper, middle, lower = bollinger(closes)
    rsi = rsi_series(closes)
    _, _, histogram = macd(closes)
    # Valid finite input can still overflow indicator arithmetic. Never leak
    # NaN/Infinity into a persisted JSON snapshot or manufacture an event.
    if not all(math.isfinite(float(value)) for series in (upper, middle, lower, histogram)
               for value in series.iloc[-3:]):
        return _empty('invalid_indicator_values')
    cur, prev, older = rows[-1], rows[-2], rows[-3]
    up, mid, lo = (float(series.iloc[-1]) for series in (upper, middle, lower))
    prev_up, prev_lo = float(upper.iloc[-2]), float(lower.iloc[-2])
    older_up, older_lo = float(upper.iloc[-3]), float(lower.iloc[-3])
    width = (up - lo) / mid
    if not math.isfinite(width):
        return _empty('invalid_indicator_values')
    events = []
    # A zero-width band on a flat series is not two simultaneous contacts.
    if up > lo:
        inside = lo < cur['close'] < up
        for side, contact, prior_contact, outside, prior_outside, older_outside in (
            ('upper', cur['high'] >= up, prev['high'] >= prev_up,
             cur['close'] > up, prev['close'] > prev_up, older['close'] > older_up),
            ('lower', cur['low'] <= lo, prev['low'] <= prev_lo,
             cur['close'] < lo, prev['close'] < prev_lo, older['close'] < older_lo),
        ):
            # A formerly zero-width band must not prevent the first real touch.
            new_contact = contact and (not prior_contact or prev_up == prev_lo)
            if new_contact:
                events.append(f'{side}_touch')
            if outside and not prior_outside:
                events.append(f'{side}_close_outside')
            if inside and (prior_outside or new_contact):
                events.append(f'{side}_rejection')
            if outside and prior_outside and not older_outside:
                events.append(f'{side}_continuation')
    rsi_now, rsi_prev = _finite(rsi.iloc[-1]), _finite(rsi.iloc[-2])
    return dict(available=True, reason='complete', bar_at=cur['timestamp'].isoformat(),
                close=cur['close'], upper=up, middle=mid, lower=lo,
                rsi=rsi_now, rsi_change=None if rsi_now is None or rsi_prev is None else rsi_now - rsi_prev,
                macd_histogram=float(histogram.iloc[-1]),
                macd_change=float(histogram.iloc[-1] - histogram.iloc[-2]),
                band_width=width,
                state=('flat_band' if up == lo else 'above_upper' if cur['close'] > up else
                       'below_lower' if cur['close'] < lo else 'inside_or_on_band'), events=events)


def build_snapshot(record, previous_bars, now):
    """Return a JSON-safe observational snapshot; never mutate caller evidence.

    Each latest completed timeframe event remains eligible for 90 seconds from
    its own candle end, allowing one skipped boundary poll to recover it.
    Stable IDs permit durable delivery deduplication. Per-timeframe events may
    remain visible as context after top-level event eligibility expires.
    """
    now = utc(now)
    snapshot = dict(version=VERSION, session_date=record.get('session_date'), as_of=now.isoformat(),
                    bar_at=None, fresh=False, reason='invalid_data', prior_complete=False, prior_days=[],
                    timeframes={tf: _empty('invalid_data') for tf in TIMEFRAMES}, events=[])

    def unavailable(reason):
        snapshot['reason'] = reason
        snapshot['timeframes'] = {tf: _empty(reason) for tf in TIMEFRAMES}
        return snapshot

    try:
        day = date.fromisoformat(record['session_date'])
        opening = session_time(day, 9, 30)
        closing = session_time(day, *market_calendar.close_time(day))
        if (now.astimezone(market_calendar.ET).date() != day or
                not market_calendar.is_trading_day(day) or not opening <= now <= closing):
            return unavailable('market_closed')
        current = _rows(record['bars'], day, now)
        if not current:
            return unavailable('no_completed_bars')
        latest_end = current[-1]['timestamp'] + _MINUTE
        snapshot['bar_at'] = current[-1]['timestamp'].isoformat()
        if not _contiguous(current, opening, latest_end):
            return unavailable('incomplete_session')
        if not 0 <= (now - latest_end).total_seconds() <= 90:
            return unavailable('stale_data')
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unavailable('invalid_data')

    prior = {}  # Accept a contiguous chain, never leap over a missing session.
    grouped = {}
    try:
        for bar in previous_bars:
            prior_day = utc(bar['timestamp']).astimezone(market_calendar.ET).date()
            grouped.setdefault(prior_day, []).append(bar)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        grouped = {}  # Unassignable optional history is not trustworthy.
    for prior_day in preceding_sessions(day):
        try:
            candidate = _rows(grouped.get(prior_day, []), prior_day, now)
            if not _contiguous(candidate, session_time(prior_day, 9, 30),
                               session_time(prior_day, *market_calendar.close_time(prior_day))):
                break
            prior[prior_day] = candidate
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            break  # Bad older history cannot poison an accepted newer session.
    # Snapshot prior_complete means at least one complete accepted session;
    # prior_days is the authoritative chronological list actually consumed.
    snapshot.update(fresh=True, reason='complete', prior_complete=bool(prior),
                    prior_days=[d.isoformat() for d in sorted(prior)])
    for tf in TIMEFRAMES:
        minutes = int(tf)
        completed = _buckets(current, day, minutes)
        if not completed:
            snapshot['timeframes'][tf] = _empty('no_completed_candle')
            continue
        rows = [bar for d in sorted(prior) for bar in _buckets(prior[d], d, minutes)] + completed
        if len(rows) < MIN_HISTORY:
            snapshot['timeframes'][tf] = dict(_empty('warming_up'),
                                              bar_at=completed[-1]['timestamp'].isoformat(),
                                              history_bars=len(rows), required_bars=MIN_HISTORY)
            continue
        frame = _frame(rows)
        snapshot['timeframes'][tf] = frame
        event_end = completed[-1]['timestamp'] + timedelta(minutes=minutes)
        if 0 <= (now - event_end).total_seconds() <= 90:
            for kind in frame['events']:
                stamp = frame['bar_at']
                snapshot['events'].append(dict(id=f'{VERSION}/{tf}/{stamp}/{kind}',
                                               timeframe=tf, kind=kind, bar_at=stamp))
    return snapshot
