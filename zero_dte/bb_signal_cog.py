"""Single-writer completed-candle BB notifications; no trading actions.

Licensed evidence is separate from retired context ledgers. Automatic delivery
is event-only. Ambiguous sends are never automatically retried; this favors
avoiding duplicates over pretending exactly-once Discord delivery is possible.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import time
import uuid

import config
from discord.ext import commands, tasks
from core import market_calendar
from core.paths import THETADATA_DATA_DIR
from core.theta_usage import audit_active
from zero_dte.orb_audit import (
    evaluate_outcome, new_record, session_time, update_record, utc, validate_record,
)
from zero_dte.bb_signals import VERSION, build_snapshot, preceding_sessions
from zero_dte.bb_signal_embeds import snapshot_embed
import discord

logger = logging.getLogger(__name__)
_MINUTE = timedelta(minutes=1)


def collection_open(now):
    local = utc(now).astimezone(market_calendar.ET)
    day = local.date()
    return (market_calendar.is_trading_day(day)
            and session_time(day, 9, 31) <= now
            <= session_time(day, *market_calendar.close_time(day)) + timedelta(minutes=5))


class BBSignalCog(commands.Cog, name='BBSignals'):
    def __init__(self, bot, theta_client, *, directory=None, audit_directory=None):
        self.bot, self.theta_client = bot, theta_client
        self.directory = Path(directory) if directory is not None else Path(THETADATA_DATA_DIR) / 'bb-events'
        self.audit_directory = (Path(audit_directory) if audit_directory is not None
                                else Path(THETADATA_DATA_DIR) / 'audit')
        self._lock = asyncio.Lock()
        self._state = None
        self._failures = 0
        self._pause_until = None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    async def cog_load(self):
        self.monitor.change_interval(seconds=max(30, getattr(config, 'SPX_CONTEXT_POLL_SECONDS', 60)))
        self.monitor.start()

    async def cog_unload(self):
        self.monitor.cancel()

    def _path(self, day):
        return self.directory / f'bb-events-{day}.json'

    @staticmethod
    def _write(path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(state, handle, allow_nan=False, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _save(self, state, now):
        self._write(self._path(state['session_date']), state)
        day = date.fromisoformat(state['session_date'])
        if audit_active(now) and audit_active(session_time(day, 9, 30)):
            self._write(self.audit_directory / f"orb-{day}.json", state['record'])
            # Separate filename/version: historical context reports must not
            # accidentally grade BB observations as old directional forecasts.
            self._write(self.audit_directory / f"bb-events-{day}.json", state)

    def _migrate_context(self, day):
        """Read durable legacy evidence, not just its optional audit mirror.

        Both sources must agree exactly if present. Even a stale mirror needs
        explicit reconciliation rather than guessing which experiment to keep.
        Never import legacy actions/deliveries into the BB ledger.
        """
        context_path = self.directory.parent / 'context' / f'context-{day}.json'
        orb_path = self.audit_directory / f'orb-{day}.json'
        record, lineage = None, {}
        if context_path.exists():
            with context_path.open(encoding='utf-8') as handle:
                legacy = json.load(handle)
            if (legacy.get('schema') != 1 or legacy.get('version') != 'spx-context-v1'
                    or legacy.get('session_date') != day.isoformat()
                    or not isinstance(legacy.get('bar_request_ids'), dict)):
                raise ValueError('Invalid legacy context migration source')
            record = legacy['record']
            validate_record(record, day)
            stamps = {bar['timestamp'] for bar in record['bars']}
            lineage = legacy['bar_request_ids']
            if any(stamp not in stamps or (rid is not None and
                   (not isinstance(rid, str) or not rid)) for stamp, rid in lineage.items()):
                raise ValueError('Invalid legacy request lineage')
        if orb_path.exists():
            with orb_path.open(encoding='utf-8') as handle:
                mirror = json.load(handle)
            validate_record(mirror, day)
            if record is not None and record != mirror:
                raise ValueError('Conflicting context and ORB migration sources')
            record = mirror
        return (record if record is not None else new_record(day)), lineage

    def _load(self, day):
        path = self._path(day)
        if not path.exists():
            # A same-session migration must not reset a frozen ORB candidate or
            # replace already collected first-seen evidence with an empty file.
            record, lineage = self._migrate_context(day)
            return dict(schema=1, version=VERSION, session_date=day.isoformat(),
                        record=record, previous_bars=[], prior_complete=False,
                        prior_sessions={d.isoformat(): dict(complete=False, retry_at=None)
                                        for d in preceding_sessions(day)},
                        bar_request_ids=lineage, prior_bar_request_ids={},
                        snapshot=None, observed_bar=None, deliveries=[], polls=0,
                        last_poll_at=None, poll_status='new',
                        requests={kind: dict(started=0, succeeded=0, failed=0)
                                  for kind in ('current', 'prior')})
        with path.open(encoding='utf-8') as handle:
            state = json.load(handle)
        if (state.get('schema') != 1 or state.get('version') != VERSION
                or state.get('session_date') != day.isoformat()
                or not isinstance(state.get('deliveries'), list)
                or not isinstance(state.get('previous_bars'), list)
                or not isinstance(state.get('bar_request_ids'), dict)
                or not isinstance(state.get('prior_bar_request_ids'), dict)):
            raise ValueError('Incompatible BB evidence')
        validate_record(state['record'], day)
        prior_days = preceding_sessions(day)
        if (not isinstance(state.get('prior_sessions'), dict)
                or set(state['prior_sessions']) != {d.isoformat() for d in prior_days}):
            raise ValueError('Invalid BB prior sessions')
        grouped = {d: [] for d in prior_days}
        for bar in state['previous_bars']:
            prior_day = utc(bar['timestamp']).astimezone(market_calendar.ET).date()
            if prior_day not in grouped:
                raise ValueError('Unexpected BB prior session')
            grouped[prior_day].append(bar)
        for prior_day, rows in grouped.items():
            validate_record(dict(new_record(prior_day), bars=rows), prior_day)
            metadata = state['prior_sessions'][prior_day.isoformat()]
            expected = int((session_time(prior_day, *market_calendar.close_time(prior_day))
                            - session_time(prior_day, 9, 30)).total_seconds() / 60)
            if (type(metadata.get('complete')) is not bool
                    or metadata['complete'] != (len(rows) == expected)):
                raise ValueError('Invalid BB prior completeness')
            if metadata['retry_at'] is not None:
                utc(metadata['retry_at'])
        if (type(state.get('prior_complete')) is not bool or state['prior_complete'] !=
                all(meta['complete'] for meta in state['prior_sessions'].values())):
            raise ValueError('Invalid BB warm-up completeness')
        for kind in ('current', 'prior'):
            counts = state['requests'][kind]
            if (any(type(counts[key]) is not int or counts[key] < 0
                    for key in ('started', 'succeeded', 'failed'))
                    or counts['succeeded'] + counts['failed'] > counts['started']):
                raise ValueError('Invalid BB request counts')
        if state['observed_bar'] is not None:
            utc(state['observed_bar'])
        seen = set()
        for entry in state['deliveries']:
            if (entry['delivery'] not in ('pending', 'sent', 'failed', 'unknown', 'suppressed')
                    or entry['snapshot']['version'] != VERSION
                    or entry['snapshot']['session_date'] != day.isoformat()
                    or not entry['event_ids']
                    or entry['event_ids'] != [event['id'] for event in entry['snapshot']['events']]
                    or len(set(entry['event_ids'])) != len(entry['event_ids'])
                    or seen.intersection(entry['event_ids'])):
                raise ValueError('Invalid BB delivery ledger')
            seen.update(entry['event_ids'])
            if entry['delivery'] == 'pending':
                entry.update(delivery='unknown', reason='interrupted_delivery')
        return state

    @staticmethod
    def _grade_orb(state, now):
        record = state['record']
        candidate = record.get('candidate')
        if candidate and candidate['status'] == 'prospective' and record['outcome'] is None:
            record['outcome'] = evaluate_outcome(
                candidate, {utc(b['timestamp']): b for b in record['bars']}, now)
            if record['outcome'] is not None:
                record['status'] = record['outcome']['status']

    def _reconcile(self, day, now):
        for path in self.directory.glob('bb-events-????-??-??.json'):
            try:
                prior = date.fromisoformat(path.stem.removeprefix('bb-events-'))
                if prior >= day:
                    continue
                with path.open(encoding='utf-8') as handle:
                    before = json.load(handle)
                state = self._load(prior)
                self._grade_orb(state, now)
                if state != before:
                    self._save(state, now)
            except Exception:
                logger.warning('Prior BB evidence could not be reconciled')

    async def _call(self, function, *args, **kwargs):
        # Shield the worker and retain the caller's collection lock even on
        # cancellation: to_thread cancellation cannot stop a provider request.
        started = time.monotonic()
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()  # Retrieve a worker failure without logging its payload.
            raise
        finally:
            if time.monotonic() - started >= 15:
                logger.warning('BB provider call exceeded 15 seconds')

    async def _prior(self, state, now):
        for day in preceding_sessions(date.fromisoformat(state['session_date'])):
            # Each session retries independently; complete sessions are never
            # fetched again, even if its neighbor is missing or a retry fails.
            now = self._now()
            if now.astimezone(market_calendar.ET).date().isoformat() != state['session_date']:
                return
            metadata = state['prior_sessions'][day.isoformat()]
            if metadata['complete'] or (metadata['retry_at'] and now < utc(metadata['retry_at'])):
                continue
            metadata['retry_at'] = (now + timedelta(minutes=15)).isoformat()
            counts = state['requests']['prior']
            counts['started'] += 1
            self._save(state, now)
            try:
                rows = await self._call(self.theta_client.index_history_ohlc, day, symbol='SPX', interval='1m',
                                       start_time='09:30:00', end_time='%02d:%02d:00' % market_calendar.close_time(day))
                received = self._now()
                request_id = getattr(self.theta_client, '_last_request_id', None)
                closing = session_time(day, *market_calendar.close_time(day))
                # Normalize in the historical session, then retain REAL receipt
                # times so history fetched later cannot appear available earlier.
                historical = new_record(day)
                update_record(historical, rows, closing, data_cutoff=closing)
                known = {bar['timestamp']: bar for bar in state['previous_bars']}
                for bar in historical['bars']:
                    if bar['timestamp'] not in known:
                        bar['available_at'] = received.isoformat()
                        known[bar['timestamp']] = bar
                        state['prior_bar_request_ids'][bar['timestamp']] = request_id
                state['previous_bars'] = [known[key] for key in sorted(known)]
                expected = int((closing - session_time(day, 9, 30)).total_seconds() / 60)
                metadata['complete'] = sum(utc(stamp).astimezone(market_calendar.ET).date() == day
                                           for stamp in known) == expected
                state['prior_complete'] = all(meta['complete'] for meta in state['prior_sessions'].values())
                counts['succeeded'] += 1
            except Exception:
                counts['failed'] += 1
                logger.warning('BB prior-session warm-up request failed')
            self._save(state, self._now())

    @staticmethod
    def _fresh(snapshot, now):
        if not snapshot.get('fresh') or not snapshot.get('bar_at'):
            return False
        local = now.astimezone(market_calendar.ET)
        return (snapshot['session_date'] == local.date().isoformat()
                and market_calendar.is_market_open(local)
                and 0 <= (now - utc(snapshot['bar_at']) - _MINUTE).total_seconds() <= 90)

    @staticmethod
    def _events_fresh(snapshot, now):
        # A newer minute must never extend an older 5m/15m event's lifetime.
        # Keep groups intact: if any member expires, suppress the whole original
        # group rather than altering durable IDs after a possible send attempt.
        try:
            return bool(snapshot['events']) and all(
                event['timeframe'] in ('1', '5', '15') and
                0 <= (now - utc(event['bar_at']) -
                      timedelta(minutes=int(event['timeframe']))).total_seconds() <= 90
                for event in snapshot['events'])
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    @staticmethod
    def _consumed(state, snapshot):
        # Indicator EWMs consume the whole accepted prefix, not only the most
        # recent minute. Incomplete current buckets and unusable prior history
        # must not get request-consumption credit.
        frames = [(int(tf), frame) for tf, frame in snapshot['timeframes'].items()
                  if snapshot['fresh'] and frame['available']]
        current_ids, prior_ids = set(), set()
        for minutes, frame in frames:
            end = utc(frame['bar_at']) + timedelta(minutes=minutes)
            current_ids.update(value for stamp, value in state['bar_request_ids'].items()
                               if utc(stamp) < end and isinstance(value, str))
            accepted_days = set(snapshot.get('prior_days', []))
            prior_ids.update(value for stamp, value in state['prior_bar_request_ids'].items()
                             if isinstance(value, str) and
                             utc(stamp).astimezone(market_calendar.ET).date().isoformat() in accepted_days)
        return {'spx': sorted(current_ids), 'prior_spx': sorted(prior_ids)}

    async def _deliver(self, state, entry):
        channel_id = getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0)
        now = self._now()
        if not self._fresh(entry['snapshot'], now) or not self._events_fresh(entry['snapshot'], now):
            entry.update(delivery='suppressed', reason='expired_before_send')
            self._save(state, now)
            return
        if not channel_id:
            entry.update(delivery='suppressed', reason='channel_disabled')
            self._save(state, now)
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            entry.update(delivery='failed', reason='channel_unavailable')
            self._save(state, now)
            return
        entry.update(delivery='pending', reason='event', attempted_at=now.isoformat())
        entry['attempts'] += 1
        self._save(state, now)  # No send unless intent is durable.
        try:
            await channel.send(embed=snapshot_embed(entry['snapshot']))
        except (discord.Forbidden, discord.NotFound):
            entry.update(delivery='failed', reason='discord_rejected')
        except Exception:
            # Timeout/network/server errors may occur after server acceptance.
            entry.update(delivery='unknown', reason='ambiguous_send')
            logger.warning('BB delivery acknowledgement unavailable')
        else:
            delivered = self._now()
            entry.update(delivery='sent', delivered_at=delivered.isoformat(),
                         reason='event' if (self._fresh(entry['snapshot'], delivered)
                                            and self._events_fresh(entry['snapshot'], delivered))
                         else 'delivered_stale')
        self._save(state, self._now())

    async def _assess(self, state):
        now = self._now()
        snapshot = build_snapshot(state['record'], state['previous_bars'], now)
        state['snapshot'] = snapshot
        stamp = snapshot['bar_at']
        backwards = (stamp and state['observed_bar'] and utc(stamp) < utc(state['observed_bar']))
        if stamp and not backwards:
            state['observed_bar'] = stamp
        self._save(state, now)
        if backwards or not self._fresh(snapshot, now):
            return
        # Retry only confirmed non-delivery while the ORIGINAL event is fresh.
        for entry in state['deliveries']:
            if entry['delivery'] == 'failed' and self._fresh(entry['snapshot'], now):
                await self._deliver(state, entry)
        seen = {event_id for entry in state['deliveries'] for event_id in entry['event_ids']}
        events = [event for event in snapshot['events'] if event['id'] not in seen]
        if not events:
            return
        outgoing = copy.deepcopy(snapshot)
        outgoing['events'] = events
        entry = dict(id=uuid.uuid4().hex, snapshot=outgoing,
                     event_ids=[event['id'] for event in events],
                     evaluated_at=now.isoformat(), delivery='pending', delivered_at=None,
                     attempted_at=None, attempts=0, reason='event',
                     consumed_request_ids=self._consumed(state, snapshot),
                     outcome_status='not_evaluated')
        state['deliveries'].append(entry)
        self._save(state, now)
        await self._deliver(state, entry)

    async def poll_once(self, now=None):
        now = now or self._now()
        if not collection_open(now):
            return
        async with self._lock:
            now = self._now()
            if not collection_open(now) or (self._pause_until and now < self._pause_until):
                return
            day = now.astimezone(market_calendar.ET).date()
            try:
                if self._state is None or self._state['session_date'] != day.isoformat():
                    self._reconcile(day, now)
                    self._state = self._load(day)
                state = self._state
                # Finalize missing ORB outcomes before recovery backfills.
                self._grade_orb(state, now)
                record = state['record']
                state['polls'] += 1
                record['polls'] += 1
                state['last_poll_at'] = record['last_poll_at'] = now.isoformat()
                state['poll_status'] = record['poll_status'] = 'request_started'
                state['requests']['current']['started'] += 1
                self._save(state, now)
            except Exception:
                self._state = None
                logger.warning('BB evidence unavailable; collection suppressed')
                return
            cutoff = min(now.replace(second=0, microsecond=0),
                         session_time(day, *market_calendar.close_time(day)))
            known = {utc(bar['timestamp']) for bar in record['bars']}
            start = session_time(day, 9, 30)
            while start in known and start < cutoff:
                start += _MINUTE
            start = min(start, cutoff - _MINUTE)
            try:
                rows = await self._call(self.theta_client.index_history_ohlc, day, symbol='SPX', interval='1m',
                                       start_time=start.astimezone(market_calendar.ET).strftime('%H:%M:%S'),
                                       end_time=cutoff.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
                received = self._now()
                if received.astimezone(market_calendar.ET).date() != day:
                    return
                before = {bar['timestamp'] for bar in record['bars']}
                update_record(record, rows, received, data_cutoff=cutoff)
                request_id = getattr(self.theta_client, '_last_request_id', None)
                for bar in record['bars']:
                    if bar['timestamp'] not in before:
                        state['bar_request_ids'][bar['timestamp']] = request_id
                state['poll_status'] = record['poll_status'] = 'success'
                state['requests']['current']['succeeded'] += 1
                self._failures = 0
                self._pause_until = None
            except Exception:
                state['poll_status'] = record['poll_status'] = 'failed'
                state['requests']['current']['failed'] += 1
                record['fetch_failures'] += 1
                self._failures += 1
                if self._failures >= 3:
                    self._pause_until = self._now() + timedelta(minutes=5)
                logger.warning('BB current-session request failed')
            # Save current evidence before optional history can block.
            self._save(state, self._now())
            self._grade_orb(state, self._now())
            await self._assess(state)
            await self._prior(state, self._now())

    @staticmethod
    def _private(ctx):
        channel_id = getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0)
        return bool(channel_id and getattr(getattr(ctx, 'channel', None), 'id', None) == channel_id)

    async def _cached(self, ctx):
        if not self._private(ctx):
            await ctx.send('BB indicators are available only in the configured private channel.')
            return
        if self._state is None:
            await ctx.send('BB indicators unavailable: no collected session. No provider request made.')
            return
        # No await during this read/computation: mutations run on the same event
        # loop. Do not wait behind a slow provider's single-flight lock merely
        # to display cached data (which may correctly have expired).
        snapshot = build_snapshot(self._state['record'], self._state['previous_bars'], self._now())
        # Status is not a new event delivery and is not counted as one.
        snapshot['events'] = []
        await ctx.send(content='Cached completed-candle indicators; no provider request made.',
                       embed=snapshot_embed(snapshot))

    @commands.command(name='bb')
    async def cmd_bb(self, ctx):
        await self._cached(ctx)

    @commands.command(name='status')
    async def cmd_status(self, ctx):
        await self._cached(ctx)

    @commands.command(name='audit')
    async def cmd_audit(self, ctx):
        """Current-session local counts only; no old context grading claims."""
        if not self._private(ctx):
            await ctx.send('Audit summaries are available only in the configured private channel.')
            return
        if self._state is None:
            await ctx.send('No BB session collected in this process. No provider request made.')
            return
        # Like status, assemble synchronously without waiting for network I/O.
        state = self._state
        counts = {kind: sum(entry['delivery'] == kind for entry in state['deliveries'])
                  for kind in ('sent', 'failed', 'unknown', 'suppressed', 'pending')}
        used = {rid for entry in state['deliveries'] if entry['delivery'] == 'sent'
                for ids in entry['consumed_request_ids'].values() for rid in ids}
        event_count = sum(len(entry['event_ids']) for entry in state['deliveries']
                          if entry['delivery'] == 'sent')
        text = (f"BB {VERSION} — session {state['session_date']} (local cached evidence)\n"
                f"Current requests: {state['requests']['current']}\n"
                f"Prior-history requests: {state['requests']['prior']}\n"
                f"Delivery messages: {counts}; sent events: {event_count}\n"
                f"Unique request IDs incorporated in sent snapshots: {len(used)}\n"
                f"Frozen ORB: {state['record']['status']} (separate paper experiment; no new flow collection).\n"
                'BB outcomes: not evaluated. No win rate, predictive edge or option P&L established.\n'
                'Request counts are attempts, not billing units; interrupted attempts may lack a result. '
                'Status views are not event deliveries. Audit expiry stops mirrors/telemetry, not index collection.')
        await ctx.send(text)

    @tasks.loop(seconds=60)
    async def monitor(self):
        await self.poll_once()

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @monitor.error
    async def monitor_error(self, _exc):
        logger.warning('BB collection loop interrupted; restarting in 30 seconds')
        await asyncio.sleep(30)
        self.monitor.restart()
