"""Single-writer SPX context collection, delivery ledger and bounded audit mirror.

Only this cog owns collection in unified mode; do not also install AuditCog.
All files contain licensed local evidence and belong under THETADATA_DATA_DIR.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
import json
import io
import logging
import os
from pathlib import Path
import time
import uuid

import discord
from discord.ext import commands, tasks

import config
from core import market_calendar
from core.paths import THETADATA_DATA_DIR
from core.theta_usage import audit_active
from zero_dte.audit_cog import capture_flow, collection_open
from zero_dte.orb_audit import new_record, update_record, validate_record, evaluate_outcome, session_time, utc
from zero_dte.spx_context import build_context, grade_context, VERSION
from zero_dte.spx_context_embeds import context_embed
from zero_dte.spx_proxy import fetch_proxy

logger = logging.getLogger(__name__)


class SPXContextCog(commands.Cog, name='SPXContext'):
    def __init__(self, bot, theta_client, *, directory=None):
        self.bot, self.theta_client = bot, theta_client
        self.directory = Path(directory) if directory is not None else Path(THETADATA_DATA_DIR) / 'context'
        self.audit_directory = Path(THETADATA_DATA_DIR) / 'audit'
        self._lock = asyncio.Lock()
        self._state = None
        self._failures = 0
        self._pause_until = None

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    async def cog_load(self):
        self.context_monitor.change_interval(seconds=max(30, getattr(config, 'SPX_CONTEXT_POLL_SECONDS', 60)))
        self.context_monitor.start()

    async def cog_unload(self):
        self.context_monitor.cancel()

    def _path(self, day):
        return self.directory / f'context-{day}.json'

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
        if audit_active(now) and audit_active(session_time(date.fromisoformat(state['session_date']), 9, 30)):
            self._write(self.audit_directory / f"context-{state['session_date']}.json", state)
            self._write(self.audit_directory / f"orb-{state['session_date']}.json", state['record'])

    def _load(self, day):
        path = self._path(day)
        if not path.exists():
            return dict(schema=1, version=VERSION, session_date=day.isoformat(), record=new_record(day),
                        previous_bars=[], prior_request_id=None, prior_retry_at=None,
                        assessments=[], polls=0, fetch_failures=0, last_poll_at=None,
                        poll_status='new', last_published_signature=None, last_published_at=None,
                        last_published_bar=None, bar_request_ids={})
        with path.open(encoding='utf-8') as handle:
            state = json.load(handle)
        if (state['schema'] != 1 or state['version'] != VERSION or state['session_date'] != day.isoformat()
                or not isinstance(state['assessments'], list) or not isinstance(state['previous_bars'], list)
                or not isinstance(state['bar_request_ids'], dict)):
            raise ValueError('Incompatible context state')
        validate_record(state['record'], day)
        for entry in state['assessments']:
            if entry['delivery'] not in ('pending', 'sent', 'suppressed', 'failed', 'unknown'):
                raise ValueError('Invalid delivery')
            if entry['delivery'] == 'pending':
                entry['delivery'] = 'unknown'
                entry['suppression_reason'] = 'interrupted_delivery'
                # A send may have succeeded before its acknowledgement was saved.
                state['last_published_signature'] = entry['assessment']['signature']
                state['last_published_at'] = entry['evaluated_at']
                state['last_published_bar'] = entry['assessment']['bar_at']
        return state

    def _grade(self, state, now):
        for entry in state['assessments']:
            if entry['delivery'] != 'sent' or all(entry['outcomes'].get(h) is not None for h in ('15', '30')):
                continue
            assessment = dict(entry['assessment'], as_of=entry['delivered_at'])
            results = grade_context(assessment, state['record']['bars'], now)
            for horizon in ('15', '30'):
                if entry['outcomes'].get(horizon) is None:
                    entry['outcomes'][horizon] = results[horizon]
        record = state['record']
        candidate = record.get('candidate')
        if candidate and candidate['status'] == 'prospective' and record['outcome'] is None:
            record['outcome'] = evaluate_outcome(candidate, {utc(b['timestamp']): b for b in record['bars']}, now)
            if record['outcome'] is not None:
                record['status'] = record['outcome']['status']

    def _reconcile(self, day, now):
        for path in self.directory.glob('context-????-??-??.json'):
            try:
                previous = date.fromisoformat(path.stem.removeprefix('context-'))
                if previous >= day:
                    continue
                with path.open(encoding='utf-8') as handle:
                    before = json.load(handle)
                state = self._load(previous)
                self._grade(state, now)
                # Don't rewrite unchanged archives every session: this avoids
                # needless Pi SD writes and preserves historical snapshot mtimes.
                if state != before:
                    self._save(state, now)
            except Exception:
                logger.warning('SPX context prior evidence could not be reconciled')

    async def _call(self, function, *args, **kwargs):
        # No wait_for: cancelling to_thread does not terminate its SDK worker.
        # The lock remains held through completion, so slow workers never fan out.
        started = time.monotonic()
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        finally:
            if time.monotonic() - started >= 15:
                logger.warning('SPX context provider call exceeded 15 seconds')

    async def _prior(self, state, now):
        if state['prior_retry_at'] and now < utc(state['prior_retry_at']):
            return
        if state.get('prior_complete'):
            return
        day = date.fromisoformat(state['session_date']) - timedelta(days=1)
        while not market_calendar.is_trading_day(day):
            day -= timedelta(days=1)
        state['prior_retry_at'] = (now + timedelta(minutes=15)).isoformat()
        try:
            rows = await self._call(self.theta_client.index_history_ohlc, day, symbol='SPX', interval='1m',
                                   start_time='09:30:00', end_time='%02d:%02d:00' % market_calendar.close_time(day))
            state['prior_request_id'] = getattr(self.theta_client, '_last_request_id', None)
            record = new_record(day)
            cutoff = session_time(day, *market_calendar.close_time(day))
            update_record(record, rows, cutoff, data_cutoff=cutoff)
            state['previous_bars'] = record['bars']
            expected = int((cutoff - session_time(day, 9, 30)).total_seconds() / 60)
            state['prior_complete'] = len(record['bars']) == expected
        except Exception:
            state['prior_request_id'] = getattr(self.theta_client, '_last_request_id', None)
            logger.warning('SPX previous-session context unavailable')

    def _reason(self, state, assessment, now):
        if not market_calendar.is_market_open(now.astimezone(market_calendar.ET)):
            return 'market_closed'
        if now < session_time(date.fromisoformat(state['session_date']), 9, 45):
            return 'opening_range_forming'
        if not getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0):
            return 'channel_disabled'
        if state['last_published_at'] is None:
            return 'first_read'
        elapsed = (now - utc(state['last_published_at'])).total_seconds()
        changed = assessment['signature'] != state['last_published_signature']
        if changed:
            setup = assessment['setup']['state']
            critical = (setup.startswith(('breakout', 'failed', 'reclaimed'))
                        or not assessment['fresh']
                        or (state['last_published_signature'][-2] in ('consider_calls', 'consider_puts')
                            and assessment.get('action', {}).get('state') != state['last_published_signature'][-2])
                        or assessment['signature'][-1] != state['last_published_signature'][-1])
            if critical or elapsed >= getattr(config, 'SPX_CONTEXT_COOLDOWN_SECONDS', 120):
                return 'state_change'
            return 'cooldown'
        if elapsed >= getattr(config, 'SPX_CONTEXT_HEARTBEAT_MINUTES', 30) * 60:
            return 'heartbeat'
        return 'unchanged'

    async def _assess(self, state, flow, proxy, request_id):
        now = self._now()
        assessment = build_context(state['record'], state['previous_bars'], now, flow, proxy)
        assessment['audit_enabled'] = audit_active(now)
        reason = self._reason(state, assessment, now)
        eligible = reason in ('first_read', 'state_change', 'heartbeat')
        entry = dict(id=uuid.uuid4().hex, assessment=assessment, evaluated_at=now.isoformat(),
                     delivery='pending' if eligible else 'suppressed', delivered_at=None,
                     suppression_reason=reason, request_ids=dict(spx=request_id,
                     prior_spx=state.get('prior_request_id'), options_flow=(flow or {}).get('request_id')),
                     outcomes={'15': None, '30': None})
        # Acquisition and consumption are different: a failed/empty latest call
        # must not receive credit for a description built from retained bars.
        entry['observed_request_ids'] = dict(entry['request_ids'])
        sources = assessment['sources']
        entry['request_ids']['spx'] = (state['bar_request_ids'].get(assessment['bar_at'])
                                     if sources['spx']['used'] else None)
        if not sources['prior_spx']['used']:
            entry['request_ids']['prior_spx'] = None
        if not sources['options_flow']['used']:
            entry['request_ids']['options_flow'] = None
        entry['consumed_request_ids'] = {
            'spx': sorted({v for v in state['bar_request_ids'].values() if isinstance(v, str)})
                   if sources['spx']['used'] else [],
            'prior_spx': [entry['request_ids']['prior_spx']] if entry['request_ids']['prior_spx'] else [],
            'options_flow': sorted({item for item in (flow or {}).get('request_ids', [entry['request_ids']['options_flow']])
                                    if isinstance(item, str)}) if sources['options_flow']['used'] else [],
        }
        state['assessments'].append(entry)
        self._save(state, now)  # Durable intent before delivery; failures prevent an untracked send.
        if not eligible:
            return
        await self._deliver(state, entry, self.bot.get_channel(getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0)))

    async def _deliver(self, state, entry, channel):
        assessment = entry['assessment']
        reason = entry['suppression_reason']
        if channel is None:
            entry.update(delivery='failed', suppression_reason='channel_unavailable')
            return
        send_at = self._now()
        if not market_calendar.is_market_open(send_at.astimezone(market_calendar.ET)):
            entry.update(delivery='suppressed', suppression_reason='market_closed')
            return
        if assessment['fresh'] and assessment['bar_at'] and (send_at - utc(assessment['bar_at']) - timedelta(minutes=1)).total_seconds() > 90:
            entry.update(delivery='suppressed', suppression_reason='expired_before_send')
            return
        try:
            await channel.send(embed=context_embed(assessment))
            delivered = self._now()
            entry.update(delivery='sent', delivered_at=delivered.isoformat(), suppression_reason=reason)
            # Grading starts after acknowledgement, never before a delayed send.
            if (delivered.astimezone(market_calendar.ET).date().isoformat() != assessment['session_date']
                    or assessment['fresh'] and assessment['bar_at'] and
                    (delivered - utc(assessment['bar_at']) - timedelta(minutes=1)).total_seconds() > 90):
                for horizon in ('15', '30'):
                    entry['outcomes'][horizon] = {'status': 'invalid_entry', 'reason': 'delivery_stale'}
            state['last_published_at'] = delivered.isoformat()
            state['last_published_signature'] = assessment['signature']
            state['last_published_bar'] = assessment['bar_at']
        except Exception:
            entry.update(delivery='failed', suppression_reason='send_failed')
            logger.warning('SPX context delivery failed')

    async def poll_once(self, now=None):
        now = now or self._now()
        if not collection_open(now):
            return
        async with self._lock:
            now = self._now()
            if not collection_open(now) or self._pause_until and now < self._pause_until:
                return
            day = now.astimezone(market_calendar.ET).date()
            try:
                if self._state is None or self._state['session_date'] != day.isoformat():
                    self._reconcile(day, now)
                    self._state = self._load(day)
                state = self._state
                # Finalize overdue gaps from evidence already saved before a
                # recovery request can backfill an otherwise missing outcome.
                self._grade(state, now)
                state['polls'] += 1
                state['last_poll_at'] = now.isoformat()
                state['poll_status'] = 'request_started'
                record = state['record']
                record['polls'] += 1
                record['last_poll_at'] = now.isoformat()
                record['poll_status'] = 'request_started'
                self._save(state, now)
            except Exception:
                self._state = None
                logger.warning('SPX context evidence unavailable; poll suppressed')
                return
            flow = capture_flow(self.bot, now)
            cutoff = min(now.replace(second=0, microsecond=0), session_time(day, *market_calendar.close_time(day)))
            known = {utc(b['timestamp']) for b in record['bars']}
            start = session_time(day, 9, 30)
            while start in known and start < cutoff:
                start += timedelta(minutes=1)
            start = min(start, cutoff - timedelta(minutes=1))
            request_id = None
            try:
                rows = await self._call(self.theta_client.index_history_ohlc, day, symbol='SPX', interval='1m',
                                       start_time=start.astimezone(market_calendar.ET).strftime('%H:%M:%S'),
                                       end_time=cutoff.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
                request_id = getattr(self.theta_client, '_last_request_id', None)
                received = self._now()
                if received.astimezone(market_calendar.ET).date() != day:
                    return
                before = {b['timestamp'] for b in record['bars']}
                update_record(record, rows, received, flow, data_cutoff=cutoff)
                for bar in record['bars']:
                    if bar['timestamp'] not in before:
                        state['bar_request_ids'][bar['timestamp']] = request_id
                record['poll_status'] = state['poll_status'] = 'success'
                self._failures = 0
            except Exception:
                request_id = getattr(self.theta_client, '_last_request_id', None)
                record['fetch_failures'] += 1
                state['fetch_failures'] += 1
                record['poll_status'] = state['poll_status'] = 'failed'
                self._failures += 1
                if self._failures >= 3:
                    self._pause_until = self._now() + timedelta(minutes=5)
                logger.warning('SPX current-session collection failed')
            # Current evidence is durable before optional requests can block.
            self._save(state, self._now())
            proxy = None
            if getattr(config, 'SPX_CONTEXT_PROXY_ENABLED', True):
                try:
                    proxy = await self._call(fetch_proxy, self._now())
                except Exception:
                    logger.warning('SPY indicative proxy unavailable')
            if self._now().astimezone(market_calendar.ET).date() != day:
                return
            self._grade(state, self._now())
            await self._assess(state, flow, proxy, request_id)
            self._save(state, self._now())
            # Prior history is lower priority: a failed/hung prior call cannot
            # prevent this poll's current assessment from being saved/delivered.
            await self._prior(state, self._now())
            self._save(state, self._now())

    async def _cached(self, ctx):
        channel_id = getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0)
        if not channel_id or getattr(getattr(ctx, 'channel', None), 'id', None) != channel_id:
            await ctx.send('SPX context is available only in the configured private context channel.')
            return
        if self._state is None or not self._state['assessments']:
            await ctx.send('SPX context unavailable: no collected assessment. No provider request was made.')
            return
        saved = self._state['assessments'][-1]['assessment']
        now = self._now()
        assessment = copy.deepcopy(saved)
        if (not market_calendar.is_market_open(now.astimezone(market_calendar.ET))
                or not saved['bar_at'] or (now - utc(saved['bar_at']) - timedelta(minutes=1)).total_seconds() > 90):
            assessment['fresh'] = False
            assessment['direction'] = 'NEUTRAL'
            for key in ('structure', 'setup', 'momentum', 'confirmation', 'invalidation'):
                assessment[key] = {'state': 'unavailable', 'text': 'Cached snapshot has expired; wait for fresh session data.'}
        if not assessment['fresh']:
            from zero_dte.spx_actions import build_action
            assessment['action'] = build_action(assessment)
            await ctx.send(content='Cached SPX snapshot — expired; no provider request made.', embed=context_embed(assessment))
            return
        # A fresh on-demand view is also a delivered observation, not a way to
        # bypass the grading ledger. Reuse cached evidence and its source IDs.
        async with self._lock:
            state = self._state
            entry = copy.deepcopy(state['assessments'][-1])
            now = self._now()
            entry.update(id=uuid.uuid4().hex, evaluated_at=now.isoformat(), delivery='pending',
                         delivered_at=None, suppression_reason='on_demand', outcomes={'15': None, '30': None})
            entry['assessment']['as_of'] = now.isoformat()
            state['assessments'].append(entry)
            self._save(state, now)
            await self._deliver(state, entry, ctx)
            self._save(state, self._now())

    @commands.command(name='spx')
    async def cmd_spx(self, ctx):
        await self._cached(ctx)

    @commands.command(name='status')
    async def cmd_status(self, ctx):
        await self._cached(ctx)

    @commands.command(name='audit')
    async def cmd_audit(self, ctx):
        """Private, read-only report; no paid endpoint calls or raw market rows."""
        channel_id = getattr(config, 'SPX_CONTEXT_CHANNEL_ID', 0)
        if not channel_id or getattr(getattr(ctx, 'channel', None), 'id', None) != channel_id:
            await ctx.send('Audit summaries are available only in the configured private context channel.')
            return
        try:
            start = date.fromisoformat(config.THETA_AUDIT_START_DATE)
            days = int(config.THETA_AUDIT_DAYS)
        except (ValueError, TypeError):
            await ctx.send('Set an explicit THETA_AUDIT_START_DATE on the Pi; no audit window is configured.')
            return
        try:
            from report_theta_audit import build_report
            text = await asyncio.to_thread(build_report, self.audit_directory, start, days, self._now())
            await ctx.send('Data fetched versus used, delivery coverage and paper outcomes — not option profits.',
                           file=discord.File(io.BytesIO(text.encode('utf-8')), filename='spx-audit.txt'))
        except Exception:
            logger.warning('SPX audit summary unavailable')
            await ctx.send('Audit summary unavailable; inspect the Pi logs. No provider request was made.')

    @tasks.loop(seconds=60)
    async def context_monitor(self):
        await self.poll_once()

    @context_monitor.before_loop
    async def before_context_monitor(self):
        await self.bot.wait_until_ready()

    @context_monitor.error
    async def context_monitor_error(self, _exc):
        logger.warning('SPX context loop interrupted; restarting in 30 seconds')
        await asyncio.sleep(30)
        self.context_monitor.restart()
