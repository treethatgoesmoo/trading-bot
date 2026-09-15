"""Read-only live-evidence reports; never reconstruct historical signal events."""
from collections import Counter
from datetime import date, timedelta
import json
import os

import discord

from core import market_calendar
from core.session_bars import session_time, utc
from core.state_store import _read, validate_state
from spy_setup.embeds import display_time


def report_date(value, now):
    today = utc(now).astimezone(market_calendar.ET).date()
    try:
        day = date.fromisoformat(value) if value else today
    except (ValueError, TypeError):
        raise ValueError('Use YYYY-MM-DD.') from None
    if day > today or not market_calendar.is_trading_day(day):
        raise ValueError('Choose today or a previous trading date.')
    return day


def load_report_state(store, day):
    # Unlike StateStore.load, missing files must not synthesize an empty day,
    # and delivery intents must not be normalized even in the returned copy.
    state = _read(store.path(day))[0]
    validate_state(state, day)
    return state


def daily_embed(state, now):
    day = date.fromisoformat(state['session_date'])
    opening = session_time(day, 9, 30)
    closing = session_time(day, *market_calendar.close_time(day))
    grouped = {}
    for observation in sorted(state['observations'], key=lambda o: utc(o['evaluated_at'])):
        c = observation['snapshot']['classification']
        if c.get('setup_id'):
            grouped.setdefault(c['setup_id'], []).append(c)
    counts = Counter()
    details = []
    for records in grouped.values():
        stages = {c['lifecycle'] for c in records}
        last = records[-1]
        counts['formed'] += 'forming' in stages
        entries = [c for c in records if c['lifecycle'] == 'triggered']
        counts['triggered'] += bool(entries)
        outcome = last['lifecycle']
        if entries:
            outcome = outcome if outcome in ('invalidated', 'target_reached', 'expired') else 'unknown'
            counts['entry_' + outcome] += 1
            entry = entries[0]
            details.append(f"{entry['direction']} | {display_time(entry.get('event_at'))} | SPY reference {entry.get('entry_reference', 'unknown')} | {outcome}")
        elif ('forming' in stages and last.get('entry_reference') is None
              and outcome in ('invalidated', 'expired')):
            counts['pre_' + outcome] += 1
        else:
            counts['unresolved'] += 1
    end = min(utc(now), closing)
    expected = max(0, int((end - opening).total_seconds() // 60))
    present = {utc(b['timestamp']) for b in state['record']['bars']}
    missing = sum(opening + timedelta(minutes=i) not in present for i in range(expected))
    late = sum((utc(b['available_at']) - utc(b['timestamp']) - timedelta(minutes=1)).total_seconds() > 90 for b in state['record']['bars'])
    deliveries = Counter(e['delivery'] for e in state['deliveries'])
    deliveries['unknown'] += deliveries.pop('pending', 0)
    embed = discord.Embed(title=f'Live daily record — {day}', description=(
        'Recorded observations only — NOT replay, trades, fills or SPX option P&L.\n'
        'Partial evidence: missing observations and outcomes are not inferred. '
        + ('Session still in progress.' if utc(now) < closing else 'Session closed.')), color=0x3498DB)
    embed.add_field(name='Observed setups', value=(
        f"Formed: {counts['formed']} | Triggered entries: {counts['triggered']}\n"
        f"Invalidated before observed entry: {counts['pre_invalidated']} | Expired before observed entry: {counts['pre_expired']}\n"
        f"Other unresolved / entry not observed: {counts['unresolved']}"), inline=False)
    embed.add_field(name='Outcomes of observed entries', value=(
        f"SPY target reached: {counts['entry_target_reached']} | Invalidated: {counts['entry_invalidated']}\n"
        f"Expired: {counts['entry_expired']} | Unknown: {counts['entry_unknown']}"), inline=False)
    embed.add_field(name='SPY entry references — not fills', value=('\n'.join(details) or 'No triggered entries recorded.')[:1024], inline=False)
    embed.add_field(name='Evidence health', value=(
        f"Completed minutes expected: {expected} | Missing: {missing} | Received >90s late: {late}\n"
        f"History ready: {state['prior_complete']} | Poll status: {state['poll_status']}\n"
        f"Last poll: {display_time(state['last_poll_at'], include_date=True)}\n"
        'Complete bars do not prove complete live observation coverage.'), inline=False)
    embed.add_field(name='Recorded alert delivery', value=' | '.join(f'{k}: {deliveries[k]}' for k in ('sent', 'failed', 'unknown', 'suppressed')), inline=False)
    embed.set_footer(text=f"{state['version']} | Pacific display; exchange session date. Unknown is not a win or loss.")
    return embed


def reserve_summary(directory, day, now):
    """Exclusive durable intent. Any existing journal means never retry this day."""
    path = directory / f'daily-delivery-{day}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf8') as handle:
            json.dump(dict(schema=1, session_date=str(day), status='unknown', attempted_at=utc(now).isoformat()), handle)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return False
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


def finish_summary(directory, day):
    # Intent stays unknown if an acknowledgement update fails. Never retry.
    path = directory / f'daily-delivery-{day}.json'
    data = _read(path)[0]
    data['status'] = 'sent'
    temporary = path.with_suffix('.tmp')
    try:
        with temporary.open('w', encoding='utf8') as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
