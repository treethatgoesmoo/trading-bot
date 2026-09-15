"""Explicit historical simulation. Never imports the live store or delivery queue."""
from datetime import date, timedelta
import re
import logging
import time

logger = logging.getLogger(__name__)

from core import market_calendar
from core.session_bars import new_record, session_time, update_record, utc
from core.thetadata_client import ThetaDataClient
from spy_setup.signals import VERSION, build_snapshot, preceding_sessions, state_key
from spy_setup.embeds import snapshot_embed

LABEL = 'REPLAY — NOT A LIVE SIGNAL'
ASSUMPTION = 'Assumes history ready before open and each candle received 5 seconds after close. Not observed live alerts, fills or option returns.'
MAX_EVENTS = 10


def replay_date(value, now):
    today = utc(now).astimezone(market_calendar.ET).date()
    if value is None:
        day = today - timedelta(days=1)
        while not market_calendar.is_trading_day(day):
            day -= timedelta(days=1)
    else:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('Use !replay [YYYY-MM-DD].')
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise ValueError('Invalid calendar date.') from None
    if not today - timedelta(days=30) <= day < today or not market_calendar.is_trading_day(day):
        raise ValueError('Choose a prior trading session within the last 30 calendar days; today/future dates are not allowed.')
    return day


def replay_session(day, now, *, client_factory=None):
    """Six sequential requests, isolated client, in-memory normalized evidence only."""
    replay_date(day.isoformat(), now)
    client = (client_factory or ThetaDataClient)(purpose='unspecified')
    records = {}
    try:
        for index, session in enumerate([day, *preceding_sessions(day)], 1):
            started = time.monotonic()
            logger.info('Replay fetching target=%s session=%s request=%d/6', day, session, index)
            closing = session_time(session, *market_calendar.close_time(session))
            bars = client.stock_history_ohlc(session, end_time=closing.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
            record = new_record(session)
            update_record(record, bars, now, data_cutoff=closing)
            expected = int((closing - session_time(session, 9, 30)).total_seconds() / 60)
            if (len(record['bars']) != expected or any(
                    utc(row['timestamp']) != session_time(session, 9, 30) + timedelta(minutes=i)
                    for i, row in enumerate(record['bars']))):
                raise ValueError('Incomplete historical session; replay aborted.')
            records[session] = record
            logger.info('Replay fetched session=%s bars=%d elapsed=%.2fs', session, len(record['bars']), time.monotonic() - started)
    finally:
        client.close()
    # Copies only: never falsify receipts in normalized evidence, even in memory.
    current = dict(records[day], bars=[dict(row, available_at=(utc(row['timestamp']) + timedelta(seconds=65)).isoformat())
                                    for row in records[day]['bars']])
    history = [dict(row, available_at=session_time(day, 9, 29).isoformat())
               for session in preceding_sessions(day) for row in records[session]['bars']]
    embeds, seen, triggered = [], set(), 0
    closing = session_time(day, *market_calendar.close_time(day))
    # Discover candidate event times, then independently recompute and apply the
    # live current-classification gate at each time. Never enqueue raw events.
    candidates = build_snapshot(current, history, closing - timedelta(seconds=1))['events']
    for event in candidates:
        at = utc(event['event_at']) + timedelta(seconds=5)
        if at >= closing:
            continue
        snapshot = build_snapshot(current, history, at)
        classification = snapshot['classification']
        if not (snapshot['fresh'] and classification.get('available') and classification.get('setup_id')):
            continue
        key = tuple(sorted(state_key(classification).items()))
        if key in seen or not 0 <= (at - utc(classification['event_at'])).total_seconds() <= 90:
            continue
        seen.add(key)
        triggered += classification['lifecycle'] == 'triggered'
        if len(embeds) < MAX_EVENTS:
            embed = snapshot_embed(snapshot)
            embed.title = f'{LABEL} | {day}\n{embed.title}'
            embed.description = f'{ASSUMPTION}\n\n{embed.description}'
            embed.set_footer(text=f'{LABEL} | {day} | {VERSION} | Unvalidated rules; historical simulation only.')
            embeds.append(embed)
    logger.info('Replay computed session=%s transitions=%d triggered=%d shown=%d', day, len(seen), triggered, len(embeds))
    return embeds, f'{LABEL} | {day} | {VERSION}\n{len(seen)} transitions; {triggered} triggered entries; {len(embeds)} shown (cap {MAX_EVENTS}).\n{ASSUMPTION}'
