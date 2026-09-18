"""Pure informational SPY levels; not entry signals or a delivery queue.

build_events(record, now) returns all currently eligible events, sorted by close
then ID. Common fields: id, version, session_date, kind, timeframe ("1" for range, "5"/"10" for BB),
event_at (UTC candle close), informational=True. Range events include range_high
and range_low; crossings also include close, previous_close and direction
("above"/"below"). BB events include period, deviations, upper, lower,
previous_upper and previous_lower. Prices refer only to SPY.

Bands use completed regular-session closes, a 20-bar SMA and population standard
deviation, including each evaluated candle in its own band. Both current and
previous bands must exist (21 candles). BB bars align to 09:30 ET and
require all constituent minutes. No previous-session warm-up or persisted state
is used. Thirty-minute BBs cannot warm up within a regular session (630 minutes).

Receipt timestamps are never altered. Missing/invalid evidence fails closed.
Only events within 90 seconds of their original close are returned; callers must
persist IDs and reserve delivery before sending, independently recheck freshness,
and respect publication opt-ins. Repeated calls intentionally return the same ID.
"""
from __future__ import annotations

from datetime import date, timedelta
from math import fsum, sqrt

from core import market_calendar
from core.session_bars import session_time, utc, validate_record

VERSION = 'spy-informational-v2'
PERIOD = 20
DEVIATIONS = 2.0
FRESH_SECONDS = 90
_MINUTE = timedelta(minutes=1)
_GRACE = timedelta(seconds=FRESH_SECONDS)


def _bands(candles):
    values = [bar['close'] for bar in candles]
    mean = fsum(values) / PERIOD
    deviation = sqrt(fsum((value - mean) ** 2 for value in values) / PERIOD)
    return mean + DEVIATIONS * deviation, mean - DEVIATIONS * deviation


def build_events(record, now):
    """Return fresh informational events only; invalid evidence returns []."""
    try:
        now = utc(now)
        day = date.fromisoformat(record['session_date'])
        opening = session_time(day, 9, 30)
        closing = session_time(day, *market_calendar.close_time(day))
        if (not market_calendar.is_trading_day(day)
                or now.astimezone(market_calendar.ET).date() != day
                or not opening <= now < closing):
            return []
        validate_record(record, day)
        rows = [bar for bar in record['bars']
                if utc(bar['available_at']) <= now and utc(bar['timestamp']) + _MINUTE <= now]
        if not rows or any(utc(bar['timestamp']) != opening + i * _MINUTE
                           for i, bar in enumerate(rows)):
            return []
        # Include prefix receipt readiness so later context cannot retroactively
        # make an old event actionable. Only bars known by now enter this prefix.
        ready = opening
        minutes = []
        for bar in rows:
            end = utc(bar['timestamp']) + _MINUTE
            receipt = utc(bar['available_at'])
            ready = max(ready, receipt)
            minutes.append(dict(end=end, close=float(bar['close']),
                                high=float(bar['high']), low=float(bar['low']),
                                receipt=receipt, ready=ready))
        events = []

        def fresh(bar):
            return (timedelta(0) <= now - bar['end'] <= _GRACE
                    and bar['ready'] <= bar['end'] + _GRACE)

        def timely(bar):
            return bar['receipt'] <= bar['end'] + _GRACE

        def emit(kind, timeframe, bar, **levels):
            stamp = bar['end'].isoformat()
            events.append(dict(id=f'{VERSION}|{day}|{kind}|{timeframe}|{stamp}',
                               version=VERSION, session_date=day.isoformat(),
                               kind=kind, timeframe=str(timeframe), event_at=stamp,
                               informational=True, **levels))

        if len(minutes) >= 15:
            high = max(bar['high'] for bar in minutes[:15])
            low = min(bar['low'] for bar in minutes[:15])
            levels = dict(range_high=high, range_low=low)
            if fresh(minutes[14]):
                emit('opening_range_ready', '1', minutes[14], **levels)
            for index in range(15, len(minutes)):
                previous, current = minutes[index - 1:index + 1]
                if not fresh(current) or not timely(previous):
                    continue
                direction = ('above' if previous['close'] <= high < current['close'] else
                             'below' if previous['close'] >= low > current['close'] else None)
                if direction:
                    emit(f'opening_range_cross_{direction}', '1', current,
                         direction=direction, close=current['close'],
                         previous_close=previous['close'], **levels)

        for timeframe in (5, 10):
            candles = []
            for start in range(0, len(minutes) - timeframe + 1, timeframe):
                block = minutes[start:start + timeframe]
                candles.append(dict(end=block[-1]['end'], close=block[-1]['close'],
                                    receipt=max(bar['receipt'] for bar in block),
                                    ready=block[-1]['ready']))
            for index in range(PERIOD, len(candles)):
                current, previous = candles[index], candles[index - 1]
                if not fresh(current) or not timely(previous):
                    continue
                upper, lower = _bands(candles[index - PERIOD + 1:index + 1])
                prev_upper, prev_lower = _bands(candles[index - PERIOD:index])
                direction = ('above' if previous['close'] <= prev_upper and current['close'] > upper else
                             'below' if previous['close'] >= prev_lower and current['close'] < lower else None)
                if direction:
                    emit(f'bb_cross_{direction}', str(timeframe), current,
                         direction=direction, close=current['close'], previous_close=previous['close'],
                         upper=upper, lower=lower, previous_upper=prev_upper,
                         previous_lower=prev_lower, period=PERIOD, deviations=DEVIATIONS)
        return sorted(events, key=lambda event: (event['event_at'], event['id']))
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return []
