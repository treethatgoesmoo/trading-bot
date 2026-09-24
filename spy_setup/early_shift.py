"""Independent, unvalidated completed-minute expansion warnings, not entries.

Reconstruct episode/cooldown state only from receipt-gated observations. Existing
setup and informational versions are untouched. No provider calls or writes.
"""
from datetime import date, timedelta
import math

from core import market_calendar
from core.session_bars import session_time, utc, validate_record
from spy_setup.signals import _baseline, _rows, preceding_sessions

VERSION = 'spy-early-shift-v1'
FRESH_SECONDS = 90
LOOKBACK_MINUTES = 10
MIN_RELATIVE_VOLUME = 1.5
COOLDOWN = timedelta(minutes=15)
_MINUTE = timedelta(minutes=1)


def build_events(record, previous_bars, now):
    """Return fresh events; replayed past candidates only enforce noise controls.

    After a warning, another in either direction must wait 15 minutes. The same
    direction additionally requires a later timely close back inside its broken
    level. Late bars/history cannot originate warnings or reset an episode.
    """
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
        rows = _rows(record['bars'], day, now)
        if not rows or any(row['timestamp'] != opening + i * _MINUTE
                           for i, row in enumerate(rows)):
            return []
        grouped = {}
        for bar in previous_bars:
            prior_day = utc(bar['timestamp']).astimezone(market_calendar.ET).date()
            grouped.setdefault(prior_day, []).append(bar)
        history = {}
        for prior_day in preceding_sessions(day):
            try:
                history[prior_day] = _rows(grouped.get(prior_day, []), prior_day, now)
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                history[prior_day] = []
        events, levels = [], {}
        last_event = None
        volume_sum = typical_sum = 0.0
        ready = opening
        for index, row in enumerate(rows):
            end = row['timestamp'] + _MINUTE
            decision = max(end, row['available_at'])
            prior_ready = ready
            ready = max(ready, row['available_at'])
            volume_sum += row['volume']
            typical_sum += ((row['high'] + row['low'] + row['close']) / 3) * row['volume']
            vwap = typical_sum / volume_sum if volume_sum else None
            if not all(math.isfinite(value) for value in (volume_sum, typical_sum)):
                return []
            if (decision - end).total_seconds() > FRESH_SECONDS or prior_ready > decision:
                continue
            for direction, level in list(levels.items()):
                if ((direction == 'above' and row['close'] <= level)
                        or (direction == 'below' and row['close'] >= level)):
                    del levels[direction]
            if index < LOOKBACK_MINUTES or end >= closing:
                continue
            prior = rows[index - LOOKBACK_MINUTES:index]
            high, low = max(bar['high'] for bar in prior), min(bar['low'] for bar in prior)
            direction = 'above' if row['close'] > high else 'below' if row['close'] < low else None
            if (direction is None or direction in levels
                    or (last_event is not None and end - last_event < COOLDOWN)):
                continue
            baseline, samples = _baseline(history, row['timestamp'], decision)
            relative_volume = row['volume'] / baseline if baseline else None
            if relative_volume is None or not math.isfinite(relative_volume) or relative_volume < MIN_RELATIVE_VOLUME:
                continue
            level = high if direction == 'above' else low
            levels[direction], last_event = level, end
            if not 0 <= (now - end).total_seconds() <= FRESH_SECONDS:
                continue
            stamp = end.isoformat()
            events.append(dict(id=f'{VERSION}|{day}|{direction}|{stamp}', version=VERSION,
                               session_date=day.isoformat(), event_at=stamp,
                               informational=True, kind=f'early_shift_{direction}', timeframe='1',
                               direction=direction, close=row['close'], level=level,
                               relative_volume=relative_volume, volume_samples=samples, vwap=vwap))
        return events
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return []
