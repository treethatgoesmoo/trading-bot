"""At-most-one-attempt delivery for informational SPY events.

Call ``await publish_events(cog, state)`` under the collector's poll/process
locks, after saving current bars and before optional history work. It returns
None; the setup ledger/schema is never mutated. Normal journal/send failures
are logged safely and contained. Cancellation propagates; an invalid state
object still raises through cog._guard.

Each event has its own exclusive journal under
informational-v1/YYYY-MM-DD/<sha256(event id)>.json. ANY existing path blocks a
retry, including corrupt, suppressed, failed or uncertain intents. This favors
lost notifications over duplicates: even a definitely rejected send is not
retried. Publication-disabled observations are durably suppressed. Events not
observed while collection is stopped cannot be recorded by this layer.

``delivery_counts(directory, day)`` reads only these journals and returns
sent/failed/unknown/suppressed counts. Missing directories mean zero records;
corrupt journals count as unknown. Directory-access errors propagate rather
than masquerading as zero. These counts are separate from setup/day reports.
"""
from __future__ import annotations

import asyncio
from datetime import date
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import tempfile

import discord

import config
from core import market_calendar
from core.safe_logging import failure_location
from core.session_bars import utc
from spy_setup.embeds import display_time
from spy_setup.informational import VERSION, FRESH_SECONDS, build_events

logger = logging.getLogger(__name__)
STATUSES = ('sent', 'failed', 'unknown', 'suppressed')
SEND_TIMEOUT_SECONDS = 30
_JOURNAL_ROOT = 'informational-v1'


def _day(value):
    return date.fromisoformat(str(value)).isoformat()


def _filename(event_id):
    return sha256(event_id.encode('utf8')).hexdigest() + '.json'


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _folder(directory, day):
    return Path(directory) / _JOURNAL_ROOT / _day(day)


def _prepare_folder(directory, day):
    # The collector already owns its evidence root. Sync the new directory
    # entries too, not just the eventual journal's contents.
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    _sync_directory(root.parent)
    folder = root
    for name in (_JOURNAL_ROOT, _day(day)):
        parent = folder
        folder = folder / name
        folder.mkdir(exist_ok=True)
        _sync_directory(parent)
    return folder


def _write_temporary(folder, data):
    fd, name = tempfile.mkstemp(prefix='.intent-', suffix='.tmp', dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as handle:
            json.dump(data, handle, allow_nan=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _reserve(directory, event, now, status, reason):
    """Atomically publish a fully written exclusive intent, then sync its entry.

    Returns (path, journal), or None if ANY path already owns the event ID.
    Hard-link creation is exclusive (unlike replace) and leaves no partially
    written JSON at the final name. Unsupported filesystems fail closed.
    """
    folder = _prepare_folder(directory, event['session_date'])
    path = folder / _filename(event['id'])
    if os.path.lexists(path):
        return None
    data = dict(schema=1, version=VERSION, session_date=event['session_date'],
                id=event['id'], event=event, status=status, reason=reason,
                observed_at=utc(now).isoformat(),
                attempted_at=utc(now).isoformat() if status == 'unknown' else None,
                delivered_at=None)
    temporary = _write_temporary(folder, data)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return None
        _sync_directory(folder)
    finally:
        temporary.unlink(missing_ok=True)
    return path, data


def _finish(reservation, status, reason, now):
    path, data = reservation
    updated = dict(data, status=status, reason=reason,
                   delivered_at=utc(now).isoformat() if status == 'sent' else None)
    temporary = _write_temporary(path.parent, updated)
    try:
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _finish_safely(reservation, status, reason, now):
    try:
        _finish(reservation, status, reason, now)
    except Exception as exc:
        # The original intent already blocks retries even if ack saving fails.
        logger.warning('Informational journal acknowledgement unavailable %s', failure_location(exc))


def _fresh(event, now):
    try:
        now = utc(now)
        local = now.astimezone(market_calendar.ET)
        return (event['version'] == VERSION and event['informational'] is True
                and event['session_date'] == local.date().isoformat()
                and market_calendar.is_market_open(local)
                and 0 <= (now - utc(event['event_at'])).total_seconds() <= FRESH_SECONDS)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return False


def _blocked(cog, event, now):
    if cog._unloading:
        return 'shutdown'
    if cog._persistence_fault:
        return 'evidence_unavailable'
    if not _fresh(event, now):
        return 'expired_before_send'
    if not cog._collection_enabled():
        return 'collection_disabled'
    if not cog._publication_enabled():
        return 'publication_disabled'
    return None


def informational_embed(event):
    """Render SPY levels in Pacific time without implying entry/fill advice."""
    kind = event['kind']
    if kind == 'opening_range_ready':
        title = 'SPY opening range ready · first 15 minutes'
    elif kind.startswith('opening_range_cross_'):
        title = f"SPY opening range · 1m close {event['direction']} range"
    else:
        title = f"SPY Bollinger Bands · {event['timeframe']}m close {event['direction']} band"
    embed = discord.Embed(title=title, color=0x3498DB, description=(
        '**INFORMATIONAL — NOT AN ENTRY SIGNAL**\n'
        f"Completed candle: {display_time(event['event_at'], include_date=True)}\n"
        'SPY prices only; not SPX option prices, fills or trade recommendations.'))
    if kind.startswith('opening_range_'):
        embed.add_field(name='SPY opening range · 9:30–9:45 ET',
                        value=f"High: ${event['range_high']:.2f}\nLow: ${event['range_low']:.2f}", inline=False)
    else:
        embed.add_field(name=f"SPY {event['timeframe']}m Bollinger Bands · 20 periods / 2σ",
                        value=f"Upper: ${event['upper']:.2f}\nLower: ${event['lower']:.2f}\n"
                              'Completed regular-session closes; population standard deviation.', inline=False)
    if 'close' in event:
        embed.add_field(name='SPY confirmed close', value=f"${event['close']:.2f}", inline=False)
    embed.set_footer(text=f'{VERSION} | Pacific display | informational levels, not setup confirmation')
    return embed


async def publish_events(cog, state):
    """Publish fresh events once at most; see module docstring for caller contract."""
    if cog._unloading or cog._persistence_fault:
        return
    cog._guard(state)
    for event in build_events(state['record'], cog._now()):
        # No await until reservation and the final policy/freshness check finish.
        # Each later event is checked again after the previous send's await.
        try:
            now = cog._now()
            reason = _blocked(cog, event, now)
            status = 'suppressed' if reason else 'unknown'
            channel = embed = None
            if reason is None:
                channel = cog.bot.get_channel(config.SPX_CHANNEL_ID)
                if channel is None:
                    status, reason = 'failed', 'channel_unavailable'
                else:
                    embed = informational_embed(event)
            reservation = _reserve(cog.directory, event, now, status, reason or 'send_reserved')
            if reservation is None or status != 'unknown':
                continue
            cog._guard(state)
            reason = _blocked(cog, event, cog._now())
            if reason:
                _finish_safely(reservation, 'suppressed', reason, cog._now())
                continue
        except Exception as exc:
            logger.warning('Informational event preparation unavailable; no send %s', failure_location(exc))
            continue
        try:
            await asyncio.wait_for(channel.send(embed=embed), timeout=SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            _finish_safely(reservation, 'unknown', 'cancelled_send', cog._now())
            raise
        except (discord.Forbidden, discord.NotFound):
            _finish_safely(reservation, 'failed', 'discord_rejected', cog._now())
        except Exception as exc:
            reason = 'send_timeout' if isinstance(exc, TimeoutError) else 'ambiguous_send'
            _finish_safely(reservation, 'unknown', reason, cog._now())
            logger.warning('Informational delivery acknowledgement unavailable %s', failure_location(exc))
        else:
            _finish_safely(reservation, 'sent', 'acknowledged', cog._now())
            logger.info('Informational event sent | kind=%s timeframe=%s session=%s',
                        event['kind'], event['timeframe'], event['session_date'])


def delivery_counts(directory, day):
    """Read-only status counts; corrupt records are unknown, not zero or sent."""
    day = _day(day)
    counts = dict.fromkeys(STATUSES, 0)
    try:
        paths = list(_folder(directory, day).iterdir())
    except FileNotFoundError:
        return counts
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            data = json.loads(path.read_text(encoding='utf8'))
            if (data['schema'] != 1 or data['version'] != VERSION
                    or data['session_date'] != day or not isinstance(data['id'], str)
                    or _filename(data['id']) != path.name
                    or data['event']['id'] != data['id']
                    or data['status'] not in STATUSES):
                raise ValueError('Invalid informational journal')
            status = data['status']
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            status = 'unknown'
        counts[status] += 1
    return counts
