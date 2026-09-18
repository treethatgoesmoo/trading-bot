"""Pure cached display context, not signal rules or a notification queue.

``build_context(record, now)`` consumes only the neutral current-session record.
``context_text(context)`` returns two short display lines (momentum, BB readiness).
The caller must rebuild at display time; a previously returned dict does not age
itself. No provider calls, writes, record mutation, or setup-version changes.

Momentum compares the last two completed session-aligned closes per timeframe;
it is an observed price change, not a trend forecast or setup confirmation.
BB "ready" means enough samples for both 20-period bands (21 candles), not that
a fresh crossing occurred or is eligible for delivery. The event engine remains
the authority for crossing/receipt/freshness and publication policy.
"""
from __future__ import annotations

from datetime import date, timedelta

from core import market_calendar
from core.session_bars import session_time, utc, validate_record
from spy_setup.informational import FRESH_SECONDS, PERIOD

TIMEFRAMES = (5, 10)
_MINUTE = timedelta(minutes=1)


def _unavailable(reason):
    return dict(available=False, reason=reason, as_of=None, momentum='unavailable',
                timeframes={str(minutes): dict(
                    candles=None, as_of=None, bb_status='unavailable',
                    bb_required=PERIOD + 1, first_ready_at=None,
                    momentum='unavailable', change=None)
                    for minutes in TIMEFRAMES})


def build_context(record, now):
    """Return JSON-compatible context; malformed, stale or gapped data fails closed.

    Top-level keys: available, reason, as_of (UTC minute close), momentum
    (up/down/flat/mixed/warming_up/unavailable), timeframes ("5", "10").
    Each timeframe has candles, as_of (its own UTC candle close), bb_status
    (ready/warming_up/session_too_short/unavailable), bb_required,
    first_ready_at (earliest possible UTC crossing close), momentum and change
    (signed SPY dollars between its last two closes, otherwise None).

    A valid empty record is warming up until the first expected minute is over
    90 seconds late. Latest-minute age, not receipt age, gates stale data. Future
    receipts and incomplete candles never enter the calculation. Full continuity
    from session open is required; trailing feed delay gets the same 90s grace
    as the informational engine. Outside market hours context is unavailable.
    """
    try:
        now = utc(now)
        day = date.fromisoformat(record['session_date'])
        validate_record(record, day)
        if now.astimezone(market_calendar.ET).date() != day:
            return _unavailable('wrong_session')
        if not market_calendar.is_trading_day(day):
            return _unavailable('market_closed')
        opening = session_time(day, 9, 30)
        closing = session_time(day, *market_calendar.close_time(day))
        if not opening <= now < closing:
            return _unavailable('market_closed')
        rows = [bar for bar in record['bars']
                if utc(bar['available_at']) <= now and utc(bar['timestamp']) + _MINUTE <= now]
        if any(utc(bar['timestamp']) != opening + index * _MINUTE
               for index, bar in enumerate(rows)):
            return _unavailable('incomplete_session')
        latest = utc(rows[-1]['timestamp']) + _MINUTE if rows else None
        if (now - (latest or opening + _MINUTE)).total_seconds() > FRESH_SECONDS:
            return _unavailable('stale_data')

        timeframes = {}
        for minutes in TIMEFRAMES:
            count = len(rows) // minutes
            first_ready = opening + timedelta(minutes=(PERIOD + 1) * minutes)
            # At-close events cannot be sent, including 10m on 13:00 ET closes.
            bb_status = ('session_too_short' if first_ready >= closing else
                         'ready' if count >= PERIOD + 1 else 'warming_up')
            change = None
            momentum = 'warming_up'
            if count >= 2:
                change = float(rows[count * minutes - 1]['close']) - float(rows[(count - 1) * minutes - 1]['close'])
                momentum = 'up' if change > 0 else 'down' if change < 0 else 'flat'
            timeframes[str(minutes)] = dict(
                candles=count, as_of=(opening + timedelta(minutes=count * minutes)).isoformat() if count else None,
                bb_status=bb_status, bb_required=PERIOD + 1,
                first_ready_at=first_ready.isoformat(), momentum=momentum, change=change)
        directions = [item['momentum'] for item in timeframes.values()]
        summary = ('warming_up' if 'warming_up' in directions else
                   directions[0] if len(set(directions)) == 1 else 'mixed')
        return dict(available=True, reason=None, as_of=latest.isoformat() if latest else None,
                    momentum=summary, timeframes=timeframes)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return _unavailable('invalid_record')


def context_text(context):
    """Render ``build_context`` output without Discord dependencies or raw prices.

    Plain text, two lines; callers may use this as a field value in status/alerts.
    Keep timeframe ``as_of`` available for a caller that needs detailed timestamps.
    """
    if not context['available']:
        reason = context['reason'].replace('_', ' ')
        return f'Momentum: unavailable ({reason})\nBB readiness: unavailable'
    momentum, readiness = [], []
    arrows = dict(up='↑', down='↓', flat='→')
    for minutes in TIMEFRAMES:
        item = context['timeframes'][str(minutes)]
        direction = item['momentum']
        if direction in arrows:
            change = item['change']
            delta = f'{change:+.2f}' if change == 0 or abs(change) >= 0.005 else f'{change:+.4g}'
            momentum.append(f'{minutes}m {arrows[direction]} ({delta})')
        else:
            momentum.append(f'{minutes}m warming up ({item["candles"]}/2)')
        count = f'{item["candles"]}/{item["bb_required"]} candles'
        status = item['bb_status']
        text = ('ready' if status == 'ready' else
                'unavailable this session (warm-up ends at/after close)' if status == 'session_too_short' else
                f'warming up ({count})')
        readiness.append(f'{minutes}m {text}')
    mixed = ' • MIXED' if context['momentum'] == 'mixed' else ''
    return (f'Momentum (completed closes): {" | ".join(momentum)}{mixed}\n'
            f'BB readiness: {" | ".join(readiness)}')
