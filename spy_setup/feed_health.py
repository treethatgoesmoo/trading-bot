"""Cached feed-liveness notices, independent of setup and informational journals.

Call await check_feed_health(cog) under the collector/process ownership locks,
including on backoff polls. No requests or setup-state mutations are performed.
Every transition is reserved durably before sending; ANY existing reservation
blocks retry. Recovery is sent only after an acknowledged stall notice. This
conservative policy can lose notices on crash/ambiguous send, never retry them.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import tempfile

import discord

import config
from core import market_calendar
from core.safe_logging import failure_location
from core.session_bars import session_time, utc, validate_record
from spy_setup.embeds import display_time

logger = logging.getLogger(__name__)
VERSION = 'feed-health-v1'
STALE_SECONDS = 90
SEND_TIMEOUT_SECONDS = 30


def _sample(cog):
    now = utc(cog._now())
    if (cog._unloading or cog._persistence_fault or not cog._collection_enabled()
            or not market_calendar.is_market_open(now.astimezone(market_calendar.ET))):
        return None
    day = now.astimezone(market_calendar.ET).date()
    if now >= session_time(day, *market_calendar.close_time(day)):
        return None
    state = cog._state
    if not state or state.get('session_date') != day.isoformat():
        raise ValueError('Current evidence unavailable')
    validate_record(state['record'], day)
    bars = [b for b in state['record']['bars'] if utc(b['available_at']) <= now
            and utc(b['timestamp']) + timedelta(minutes=1) <= now]
    latest = utc(bars[-1]['timestamp']) + timedelta(minutes=1) if bars else None
    anchor = latest or session_time(day, 9, 31)
    # No warning until the first expected close is more than 90 seconds late.
    stalled = (now - anchor).total_seconds() > STALE_SECONDS
    if latest is None and not stalled:
        return None
    return dict(day=day.isoformat(), now=now, latest=latest, stalled=stalled)


def _sync(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _folder(root, day):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('Evidence root missing')
    folder = root
    for part in (VERSION, day):
        child = folder / part
        child.mkdir(exist_ok=True)
        _sync(folder)
        folder = child
    return folder


def _write(path, data, *, exclusive=False):
    fd, name = tempfile.mkstemp(prefix='.health-', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temp, path)
        else:
            os.replace(temp, path)
        _sync(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _history(folder, day):
    last = None
    for index, path in enumerate(sorted(folder.glob('*.json')), 1):
        item = json.loads(path.read_text(encoding='utf8'))
        if (path.name != f'{index:06d}.json' or item['sequence'] != index
                or item['version'] != VERSION or item['session_date'] != day
                or item['kind'] != ('stall' if index % 2 else 'recovery')
                or item['status'] not in ('unknown', 'sent', 'failed', 'suppressed')):
            raise ValueError('Invalid health journal')
        utc(item['observed_at'])
        last = item
    return last


def _finish(path, item, status, reason):
    item = dict(item, status=status, reason=reason)
    try:
        _write(path, item)
    except Exception as exc:
        logger.warning('Feed health acknowledgement not persisted | event=%s %s',
                       path.stem, failure_location(exc))
    logger.info('Feed health delivery | session=%s event=%s kind=%s status=%s reason=%s',
                item['session_date'], path.stem, item['kind'], status, reason)


async def check_feed_health(cog):
    """Observe one transition at most; normal failures fail closed, cancellation propagates.

    Suppressed stalls are persisted, so enabling publication mid-outage does not
    replay them. Recovery is suppressed unless that outage's stall was durably
    acknowledged sent. Freshness is candle age, not fetch success or setup state.
    A fresh latest candle does not claim that missing historical gaps are repaired.
    """
    try:
        sample = _sample(cog)
        if sample is None:
            return
        folder = _folder(cog.directory, sample['day'])
        last = _history(folder, sample['day'])
        was_stalled = last is not None and last['kind'] == 'stall'
        if sample['stalled'] == was_stalled:
            return
        kind = 'stall' if sample['stalled'] else 'recovery'
        sequence = last['sequence'] + 1 if last else 1
        reason = ('publication_disabled' if not cog._publication_enabled() else
                  'stall_not_acknowledged' if kind == 'recovery' and last['status'] != 'sent' else None)
        channel = cog.bot.get_channel(config.SPX_CHANNEL_ID) if reason is None and config.SPX_CHANNEL_ID else None
        if reason is None and channel is None:
            reason = 'channel_unavailable'
        item = dict(version=VERSION, session_date=sample['day'], sequence=sequence,
                    kind=kind, observed_at=sample['now'].isoformat(),
                    status='suppressed' if reason else 'unknown', reason=reason or 'send_reserved')
        path = folder / f'{sequence:06d}.json'
        _write(path, item, exclusive=True)
        logger.info('Feed health transition reserved | session=%s event=%s kind=%s reason=%s',
                    sample['day'], path.stem, kind, item['reason'])
        if reason:
            return
        # Recheck policy/market and evidence at the actual send boundary.
        current = _sample(cog)
        if (current is None or current['day'] != sample['day']
                or current['stalled'] != sample['stalled'] or not cog._publication_enabled()):
            _finish(path, item, 'suppressed', 'changed_before_send')
            return
        latest = display_time(current['latest']) if current['latest'] else 'No completed minute received'
        embed = discord.Embed(
            title='⚠️ SPY FEED STALE' if kind == 'stall' else '✅ SPY FEED FRESH AGAIN',
            color=0xF1C40F if kind == 'stall' else 0x2ECC71,
            description=(f'Last candle: {latest}' if kind == 'stall' else
                         f'Latest: {latest} · Historical gaps may remain'))
    except Exception as exc:
        logger.warning('Feed health unavailable; no send %s', failure_location(exc))
        return
    try:
        await asyncio.wait_for(channel.send(embed=embed), timeout=SEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        _finish(path, item, 'unknown', 'cancelled_send')
        raise
    except (discord.Forbidden, discord.NotFound):
        _finish(path, item, 'failed', 'discord_rejected')
    except Exception as exc:
        _finish(path, item, 'unknown', 'send_timeout' if isinstance(exc, TimeoutError) else 'ambiguous_send')
        logger.warning('Feed health send acknowledgement unavailable %s', failure_location(exc))
    else:
        _finish(path, item, 'sent', 'acknowledged')
