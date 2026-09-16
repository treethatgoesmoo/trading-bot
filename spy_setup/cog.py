"""Completed-candle SPY setups for SPX options, with durable transition delivery.

Observed state, not last successful delivery, is the transition baseline. A
suppressed/unknown transition is never replayed after enabling publication or a
restart. Only confirmed non-delivery may retry while the original decision is
fresh and still the current observation. Status is cached and never a new alert.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
import logging
from pathlib import Path
import time

import config
import discord
from discord.ext import commands, tasks

from core import market_calendar
from core.paths import THETADATA_DATA_DIR
from core.process_lock import ProcessLock
from core.session_bars import new_record, session_time, update_record, utc
from core.state_store import StateStore, observation_id, state_key
from spy_setup.signals import VERSION, build_snapshot, preceding_sessions
from spy_setup.embeds import snapshot_embed, display_time

logger = logging.getLogger(__name__)
_MINUTE = timedelta(minutes=1)


class EvidenceError(RuntimeError):
    """The in-memory state cannot be used again until durable evidence reloads."""


def collection_open(now):
    now = utc(now)
    day = now.astimezone(market_calendar.ET).date()
    return (market_calendar.is_trading_day(day) and session_time(day, 9, 31) <= now
            <= session_time(day, *market_calendar.close_time(day)) + timedelta(minutes=5))


class SpySetupCog(commands.Cog, name='SpySetup'):
    def __init__(self, bot, theta_client=None, *, directory=None):
        self.bot, self.theta_client = bot, theta_client
        self._replay_next = {}
        self.directory = Path(directory) if directory is not None else Path(THETADATA_DATA_DIR) / 'spy-setups'
        self.store = StateStore(self.directory)
        self._ownership = ProcessLock(self.directory)
        self._lock = asyncio.Lock()
        self._state = None
        self._persistence_fault = False
        self._failures = 0
        self._pause_until = None
        self._replay_tasks = set()
        self._unloading = False

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    def _collection_enabled(self):
        # Missing credentials should yield a None client from assembly. Check
        # both here too: a disabled flag never means "collect anyway for status".
        return (getattr(config, 'COLLECTION_ENABLED', False) is True
                and bool(getattr(config, 'THETADATA_API_KEY', '')) and self.theta_client is not None)

    @staticmethod
    def _publication_enabled():
        return (getattr(config, 'PUBLICATION_ENABLED', False) is True
                and bool(getattr(config, 'SPX_CHANNEL_ID', 0)))

    async def cog_load(self):
        # No provider work on installation, including when collection is off.
        self._ownership.acquire()
        try:
            self.monitor.change_interval(seconds=max(30, getattr(config, 'SPX_POLL_SECONDS', 60)))
            self.monitor.start()
        except BaseException:
            self._ownership.release()
            raise

    async def cog_unload(self):
        self._unloading = True
        replay_tasks = list(self._replay_tasks)
        for replay_task in replay_tasks:
            replay_task.cancel()
        for replay_task in replay_tasks:
            while not replay_task.done():
                try:
                    await asyncio.shield(replay_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        task = self.monitor.get_task()
        self.monitor.cancel()
        # Cancellation cannot stop a provider thread. Retain process ownership
        # until the loop's shielded worker has actually finished.
        if task is not None:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
        try:
            if self.theta_client is not None:
                await self._call(self.theta_client.close)
        finally:
            self._ownership.release()

    def _load(self, day):
        try:
            state = self.store.load(day)
        except Exception as exc:
            self._state = None
            self._persistence_fault = True
            raise EvidenceError('SPY setup evidence could not be loaded') from exc
        self._state = state
        self._persistence_fault = False
        return state

    def _guard(self, state):
        if self._persistence_fault or state is not self._state:
            raise EvidenceError('Durable reload required before further state mutation')

    def _save(self, state, now=None):
        self._guard(state)
        try:
            self.store.save(state)
        except Exception as exc:
            # In-memory counters/intent may be ahead of disk. Never send or
            # continue with this object after an unsuccessful persistence step.
            self._state = None
            self._persistence_fault = True
            raise EvidenceError('SPY setup evidence could not be saved') from exc

    async def _call(self, function, *args, **kwargs):
        started = time.monotonic()
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # to_thread cancellation does not stop the SDK. Hold the poll lock
            # until its worker finishes, even if cancellation is repeated.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
        finally:
            if time.monotonic() - started >= 15:
                logger.warning('SPY provider call exceeded 15 seconds')

    async def _prior(self, state, now=None):
        self._guard(state)
        day = date.fromisoformat(state['session_date'])
        for prior_day in preceding_sessions(day):
            now = self._now()
            if (not self._collection_enabled() or not collection_open(now)
                    or now.astimezone(market_calendar.ET).date() != day):
                return
            metadata = state['prior_sessions'][prior_day.isoformat()]
            if metadata['complete'] or (metadata['retry_at'] and now < utc(metadata['retry_at'])):
                continue
            metadata['retry_at'] = (now + timedelta(minutes=15)).isoformat()
            counts = state['requests']['prior']
            counts['started'] += 1
            self._save(state)
            try:
                closing = session_time(prior_day, *market_calendar.close_time(prior_day))
                rows = await self._call(self.theta_client.stock_history_ohlc, prior_day,
                                       symbol='SPY', interval='1m', start_time='09:30:00',
                                       end_time=closing.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
                received = self._now()
                if received.astimezone(market_calendar.ET).date() != day:
                    return  # Durable unmatched request start, not a fabricated failure.
                historical = new_record(prior_day)
                update_record(historical, rows, received, data_cutoff=closing)
                request_id = self._request_id()
                known = {b['timestamp']: b for b in state['previous_bars']}
                for bar in historical['bars']:
                    if bar['timestamp'] not in known:
                        known[bar['timestamp']] = bar
                        state['prior_bar_request_ids'][bar['timestamp']] = request_id
                state['previous_bars'] = [known[key] for key in sorted(known)]
                expected = int((closing - session_time(prior_day, 9, 30)).total_seconds() / 60)
                metadata['complete'] = sum(utc(key).astimezone(market_calendar.ET).date() == prior_day
                                           for key in known) == expected
                state['prior_complete'] = all(meta['complete'] for meta in state['prior_sessions'].values())
                counts['succeeded'] += 1
            except Exception:
                counts['failed'] += 1
                logger.warning('SPY prior-session warm-up request failed')
            self._save(state)

    def _request_id(self):
        value = getattr(self.theta_client, '_last_request_id', None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _fresh(snapshot, now):
        try:
            now = utc(now)
            return (snapshot['fresh'] is True and snapshot['session_date'] ==
                    now.astimezone(market_calendar.ET).date().isoformat()
                    and market_calendar.is_market_open(now.astimezone(market_calendar.ET))
                    and 0 <= (now - utc(snapshot['bar_at']) - _MINUTE).total_seconds() <= 90)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    @classmethod
    def _decision_fresh(cls, snapshot, now):
        try:
            classification = snapshot['classification']
            state_key(classification)
            return (cls._fresh(snapshot, now) and classification['available'] is True and classification.get('setup_id') is not None
                    and 0 <= (utc(now) - utc(classification['event_at'])).total_seconds() <= 90)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False

    @staticmethod
    def _consumed(state, snapshot):
        current, prior = set(), set()
        for tf, frame in snapshot['timeframes'].items():
            if not snapshot['fresh'] or not frame.get('available'):
                continue
            end = utc(frame['bar_at']) + timedelta(minutes=int(tf))
            current.update(value for stamp, value in state['bar_request_ids'].items()
                           if utc(stamp) < end and isinstance(value, str))
        accepted = set(snapshot.get('prior_days', []))
        prior.update(value for stamp, value in state['prior_bar_request_ids'].items()
                     if isinstance(value, str) and
                     utc(stamp).astimezone(market_calendar.ET).date().isoformat() in accepted)
        return dict(spy=sorted(current), prior_spy=sorted(prior))

    @staticmethod
    def _observation(state, entry):
        return next(item for item in state['observations'] if item['id'] == entry['observation_id'])

    def _still_current(self, state, item):
        snapshot = state.get('snapshot') or {}
        classification = snapshot.get('classification') or {}
        return (state['observed_bar'] == item['snapshot']['classification']['bar_at']
                and classification.get('available') is True
                and classification['bar_at'] == state['observed_bar']
                and state_key(classification) == state['last_observed_state']
                and self._decision_fresh(snapshot, self._now()))

    async def _deliver(self, state, entry):
        self._guard(state)
        item = self._observation(state, entry)
        now = self._now()
        if not self._decision_fresh(item['snapshot'], now):
            entry.update(delivery='suppressed', reason='expired_before_send')
        elif not self._still_current(state, item):
            entry.update(delivery='suppressed', reason='superseded_or_unavailable')
        elif not self._collection_enabled() or not self._publication_enabled():
            entry.update(delivery='suppressed', reason='publication_disabled')
        else:
            channel = self.bot.get_channel(getattr(config, 'SPX_CHANNEL_ID', 0))
            if channel is None:
                entry.update(delivery='failed', reason='channel_unavailable')
            else:
                embed = snapshot_embed(item['snapshot'])  # Render before persisting a send attempt.
                # Rendering/lookup may take time. Recheck immediately at the
                # boundary; candle freshness cannot be prolonged by a new 1m bar.
                now = self._now()
                if not self._decision_fresh(item['snapshot'], now):
                    entry.update(delivery='suppressed', reason='expired_before_send')
                    self._save(state)
                    return
                entry.update(delivery='pending', reason='state_transition', attempted_at=now.isoformat())
                entry['attempts'] += 1
                self._save(state)  # Durable intent strictly precedes Discord.
                # A slow fsync or runtime disable must not authorize an expired
                # send merely because it was fresh before persistence.
                if not self._decision_fresh(item['snapshot'], self._now()):
                    entry.update(delivery='suppressed', reason='expired_before_send')
                    self._save(state)
                    return
                if not self._collection_enabled() or not self._publication_enabled():
                    entry.update(delivery='suppressed', reason='publication_disabled')
                    self._save(state)
                    return
                try:
                    await channel.send(embed=embed)
                except asyncio.CancelledError:
                    entry.update(delivery='unknown', reason='cancelled_send')
                    try:
                        self._save(state)
                    finally:
                        raise
                except (discord.Forbidden, discord.NotFound):
                    entry.update(delivery='failed', reason='discord_rejected')
                except Exception:
                    entry.update(delivery='unknown', reason='ambiguous_send')
                    logger.warning('SPX delivery acknowledgement unavailable')
                else:
                    delivered = self._now()
                    entry.update(delivery='sent', delivered_at=delivered.isoformat(),
                                 reason='state_transition' if self._decision_fresh(item['snapshot'], delivered)
                                 else 'delivered_stale')
                    state['last_acknowledged_state'] = state_key(item['snapshot']['classification'])
                # CancelledError bypasses this save: durable pending recovers as
                # unknown, never as a definitely failed/automatically retried send.
        self._save(state)

    async def _assess(self, state):
        self._guard(state)
        now = self._now()
        snapshot = build_snapshot(state['record'], state['previous_bars'], now)
        state['snapshot'] = snapshot
        classification = snapshot['classification']
        stamp = classification.get('bar_at')
        backwards = bool(stamp and state['observed_bar'] and utc(stamp) < utc(state['observed_bar']))
        if backwards:
            snapshot['fresh'], snapshot['reason'] = False, 'backward_decision'
            snapshot['classification'] = dict(available=False, reason='backward_decision', bar_at=None,
                                               direction='unavailable', reasons=[])
        if not backwards and self._decision_fresh(snapshot, now):
            if state['observed_bar'] is None or utc(stamp) > utc(state['observed_bar']):
                key, previous = state_key(classification), state['last_observed_state']
                transition = key != previous if previous is not None else key['setup_id'] is not None
                item = dict(id=observation_id(state['session_date'], classification),
                            snapshot=copy.deepcopy(snapshot), evaluated_at=snapshot['as_of'],
                            previous_state=copy.deepcopy(previous), transition=bool(transition),
                            consumed_request_ids=self._consumed(state, snapshot), outcome_status='not_evaluated')
                state['observations'].append(item)
                state['observed_bar'], state['last_observed_state'] = stamp, key
                if transition:
                    state['deliveries'].append(dict(id=item['id'], observation_id=item['id'],
                                                    delivery='pending', attempted_at=None, attempts=0,
                                                    delivered_at=None, reason='state_transition'))
        # Resolve expired/superseded failed intents before any new send. A
        # changed or unavailable snapshot can never authorize an older retry.
        for entry in state['deliveries']:
            if entry['delivery'] not in ('failed', 'pending'):
                continue
            item = self._observation(state, entry)
            if not self._decision_fresh(item['snapshot'], now):
                entry.update(delivery='suppressed', reason='expired_before_send')
            elif not self._still_current(state, item):
                entry.update(delivery='suppressed', reason='superseded_or_unavailable')
        self._save(state)
        if not self._decision_fresh(snapshot, self._now()):
            return
        for entry in state['deliveries']:
            if entry['delivery'] in ('failed', 'pending'):
                await self._deliver(state, entry)

    async def poll_once(self, now=None):
        if not self._collection_enabled() or not collection_open(now or self._now()):
            return
        async with self._lock:
            now = self._now()
            if (not self._collection_enabled() or not collection_open(now)
                    or (self._pause_until and now < self._pause_until)):
                return
            day = now.astimezone(market_calendar.ET).date()
            try:
                if self._state is None or self._state['session_date'] != day.isoformat():
                    self._load(day)
                state, record = self._state, self._state['record']
                state['polls'] += 1
                record['polls'] += 1
                state['last_poll_at'] = record['last_poll_at'] = now.isoformat()
                state['poll_status'] = 'request_started'
                state['requests']['current']['started'] += 1
                self._save(state)
                cutoff = min(now.replace(second=0, microsecond=0),
                             session_time(day, *market_calendar.close_time(day)))
                known = {utc(bar['timestamp']) for bar in record['bars']}
                start = session_time(day, 9, 30)
                while start in known and start < cutoff:
                    start += _MINUTE
                start = min(start, cutoff - _MINUTE)
                try:
                    rows = await self._call(self.theta_client.stock_history_ohlc, day,
                                           symbol='SPY', interval='1m',
                                           start_time=start.astimezone(market_calendar.ET).strftime('%H:%M:%S'),
                                           end_time=cutoff.astimezone(market_calendar.ET).strftime('%H:%M:%S'))
                    received = self._now()
                    if received.astimezone(market_calendar.ET).date() != day:
                        return
                    before = {bar['timestamp'] for bar in record['bars']}
                    update_record(record, rows, received, data_cutoff=cutoff)
                    request_id = self._request_id()
                    for bar in record['bars']:
                        if bar['timestamp'] not in before:
                            state['bar_request_ids'][bar['timestamp']] = request_id
                    state['poll_status'] = 'success'
                    state['requests']['current']['succeeded'] += 1
                    self._failures, self._pause_until = 0, None
                except Exception:
                    state['poll_status'] = 'failed'
                    state['requests']['current']['failed'] += 1
                    record['fetch_failures'] += 1
                    self._failures += 1
                    if self._failures >= 3:
                        self._pause_until = self._now() + timedelta(minutes=5)
                    logger.warning('SPY current-session request failed')
                self._save(state)  # Save current bars before optional history can block.
                await self._assess(state)
                # Independent informational levels do not require setup/VWAP/
                # relative-volume confirmation or historical warm-up.
                from spy_setup.informational_delivery import publish_events
                await publish_events(self, state)
                await self._prior(state)
                # History may have become ready within this same fresh1m window.
                await self._assess(state)
            except EvidenceError:
                logger.warning('SPX evidence unavailable; no further requests or publication this poll')

    @staticmethod
    def _private(ctx):
        channel_id = getattr(config, 'SPX_CHANNEL_ID', 0)
        return bool(channel_id and getattr(getattr(ctx, 'channel', None), 'id', None) == channel_id)

    @commands.command(name='preview', help='Show five fictional setup alerts; no market requests or live state changes.')
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def preview(self, ctx):
        if not self._private(ctx):
            await ctx.send('Setup previews are available only in the configured private channel.')
            return
        from spy_setup.preview import DEMO, preview_embeds
        await ctx.send(content=f'{DEMO}\nForming → triggered, followed by three ALTERNATIVE endings. '
                               'All prices and times are fictional. CALL examples shown; PUT setups reverse direction.',
                       embeds=preview_embeds())

    @commands.command(name='replay', help='Replay a prior session: !replay [YYYY-MM-DD]. Makes up to six historical requests.')
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    async def replay(self, ctx, session: str = None):
        logger.info('Replay requested')
        if not self._private(ctx):
            logger.info('Replay rejected: private_channel_required')
            await ctx.send('Historical replay is available only in the configured private channel.')
            return
        from spy_setup.replay import LABEL, replay_date, replay_session
        try:
            day = replay_date(session, self._now())
        except ValueError as exc:
            logger.info('Replay rejected: invalid_session')
            await ctx.send(f'{LABEL}: {exc}')
            return
        if self._unloading or not getattr(config, 'THETADATA_API_KEY', ''):
            logger.info('Replay rejected: shutdown_or_missing_credentials')
            await ctx.send(f'{LABEL}: unavailable during shutdown or without provider credentials.')
            return
        # No await between validation and cooldown reservation. Invalid requests
        # never reset or consume another invocation's reservation.
        clock = time.monotonic()
        remaining = self._replay_next.get(ctx.channel.id, 0) - clock
        if remaining > 0:
            raise commands.CommandOnCooldown(commands.Cooldown(1, 300), remaining, commands.BucketType.channel)
        self._replay_next[ctx.channel.id] = clock + 300
        started = time.monotonic()
        logger.info('Replay accepted session=%s', day)
        task = asyncio.current_task()
        self._replay_tasks.add(task)
        try:
            await ctx.send(f'{LABEL} | {day}: fetching target plus five prior sessions (up to six requests). '
                           'Historical simulation only; no live-state changes. Times are Pacific.')
            try:
                embeds, summary = await self._call(replay_session, day, self._now())
            except Exception as exc:
                from core.safe_logging import failure_location
                logger.error('Replay failed session=%s elapsed=%.2fs %s', day, time.monotonic() - started, failure_location(exc))
                await ctx.send(f'{LABEL} | {day}: unavailable—historical access, data completeness or provider failure. No replay alerts generated.')
                return
            # One embed per send avoids Discord's combined 6000-character limit.
            # Send failures are not automatically retried.
            for index, embed in enumerate(embeds, 1):
                logger.info('Replay sending session=%s embed=%d/%d', day, index, len(embeds))
                await ctx.send(embed=embed)
            await ctx.send(summary)
            logger.info('Replay delivered session=%s embeds=%d elapsed=%.2fs', day, len(embeds), time.monotonic() - started)
        finally:
            self._replay_tasks.discard(task)

    @replay.error
    async def replay_error(self, ctx, error):
        import math
        from core.safe_logging import failure_location
        error = getattr(error, 'original', error)
        if isinstance(error, commands.CommandOnCooldown):
            seconds = math.ceil(error.retry_after)
            logger.info('Replay blocked: cooldown remaining=%ds', seconds)
            await ctx.send(f'Replay cooldown: try again in {seconds} seconds.')
        elif isinstance(error, commands.MaxConcurrencyReached):
            logger.info('Replay blocked: already_running')
            await ctx.send('A replay is already running. Please wait for it to finish.')
        elif isinstance(error, commands.UserInputError):
            logger.info('Replay rejected: invalid_arguments')
            await ctx.send('Use !replay [YYYY-MM-DD] with a prior trading date.')
        else:
            logger.error('Replay command failed %s', failure_location(error))
            await ctx.send('Replay failed unexpectedly. Check the local bot log. Messages already sent will not be retried.')

    @commands.command(name='day', help='Read saved live observations: day [YYYY-MM-DD]. No historical requests or simulation.')
    async def day(self, ctx, session: str = None):
        if not self._private(ctx):
            await ctx.send('Daily reports are available only in the configured private channel.')
            return
        from spy_setup.daily import report_date, load_report_state, daily_embed
        try:
            day = report_date(session, self._now())
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        try:
            state = load_report_state(self.store, day)
            embed = daily_embed(state, self._now())
            self._add_informational_counts(embed, day)
        except Exception:
            await ctx.send(f'Daily report {day} unavailable: missing, incompatible or invalid saved evidence. No simulation performed.')
            return
        await ctx.send(embed=embed)

    @commands.command(name='cmd', help='List current registered commands and their usage.')
    async def cmd(self, ctx):
        if not self._private(ctx):
            await ctx.send('Command guide is available only in the configured private channel.')
            return
        prefix = getattr(ctx, 'clean_prefix', None) or config.COMMAND_PREFIX
        lines = []
        for command in sorted(self.bot.walk_commands(), key=lambda c: c.qualified_name):
            if command.hidden or not command.enabled:
                continue
            lines.append(f'`{prefix}{command.qualified_name} {command.signature}` — {command.short_doc or "No description available."}')
        # Bounded chunks preserve Discord's message limit as commands evolve.
        text = 'Current commands (configured private channel):\n'
        for line in lines:
            if len(text) + len(line) > 1900:
                await ctx.send(text)
                text = ''
            text += line[:1800] + '\n'
        if text:
            await ctx.send(text)

    async def daily_once(self):
        from spy_setup.daily import load_report_state, daily_embed, reserve_summary, finish_summary
        now = self._now()
        day = now.astimezone(market_calendar.ET).date()
        if (self._unloading or self._persistence_fault or not self._collection_enabled()
                or not self._publication_enabled() or not market_calendar.is_trading_day(day)):
            return
        closing = session_time(day, *market_calendar.close_time(day))
        # Current session only, after final collection opportunity; no next-day catch-up.
        if not closing + timedelta(minutes=6) <= now < closing + timedelta(minutes=30):
            return
        async with self._lock:
            now = self._now()
            if (self._unloading or self._persistence_fault or not self._collection_enabled()
                    or not self._publication_enabled()
                    or not closing + timedelta(minutes=6) <= now < closing + timedelta(minutes=30)):
                return
            try:
                state = load_report_state(self.store, day)
                embed = daily_embed(state, now)
                self._add_informational_counts(embed, day)
                channel = self.bot.get_channel(config.SPX_CHANNEL_ID)
                if channel is None:
                    return
                if not reserve_summary(self.directory, day, now):
                    return
            except Exception:
                logger.warning('Daily report unavailable; evidence/intent could not be prepared')
                return
            try:
                await asyncio.wait_for(channel.send(embed=embed), timeout=30)
                finish_summary(self.directory, day)
                logger.info('Daily report sent | session=%s', day)
            except asyncio.CancelledError:
                # Reserved journal remains unknown; shutdown retains ownership until here.
                raise
            except Exception:
                logger.warning('Daily report delivery unknown; no automatic retry | session=%s', day)

    def _informational_counts(self, day):
        from spy_setup.informational_delivery import delivery_counts
        try:
            counts = delivery_counts(self.directory, day)
            return ' | '.join(f'{kind}: {count}' for kind, count in counts.items())
        except Exception:
            return 'unavailable — informational delivery evidence could not be read'

    def _add_informational_counts(self, embed, day):
        embed.add_field(name='Informational range / BB notifications',
                        value=self._informational_counts(day) +
                        '\nSeparate from setup counts; missing journals do not prove live coverage.',
                        inline=False)

    @commands.command(name='status', aliases=[], help='Show cached live setup and data/delivery health; no provider requests.')
    async def cmd_status(self, ctx):
        """Cached state and operational health, restricted to the private channel."""
        if not self._private(ctx):
            await ctx.send('SPX status is available only in the configured private channel.')
            return
        now = self._now()
        # Do not wait for a slow network worker or fetch on a command.
        state = self._state
        collecting = self._collection_enabled()
        requested = getattr(config, 'PUBLICATION_ENABLED', False) is True
        publishing = collecting and self._publication_enabled()
        blocked = ('collection flag off' if not getattr(config, 'COLLECTION_ENABLED', False) else
                   'credentials missing' if not getattr(config, 'THETADATA_API_KEY', '') else
                   'provider unavailable' if self.theta_client is None else
                   'publication flag off' if not requested else
                   'destination missing' if not getattr(config, 'SPX_CHANNEL_ID', 0) else 'none')
        if state is None:
            snapshot = build_snapshot(new_record(now.astimezone(market_calendar.ET).date()), [], now)
        else:
            snapshot = build_snapshot(state['record'], state['previous_bars'], now)
        health = ('evidence_error' if self._persistence_fault else 'collection_off' if not collecting else
                  'market_closed' if not market_calendar.is_market_open(now.astimezone(market_calendar.ET)) else
                  'no_session' if state is None else 'provider_error' if state['poll_status'] == 'failed' else
                  'stale_or_missing' if not snapshot['fresh'] else
                  'warming_up' if not snapshot['classification']['available'] else 'healthy')
        snapshot['operational'] = dict(health=health, collection_enabled=collecting,
                                        publication_enabled=publishing, poll_status=state['poll_status'] if state else 'new')
        counts = {kind: sum(e['delivery'] == kind for e in state['deliveries']) if state else 0
                  for kind in ('sent', 'failed', 'unknown', 'suppressed', 'pending')}
        latest = snapshot.get('bar_at')
        age = f"{(now - utc(latest) - _MINUTE).total_seconds():.0f}s" if latest else 'unavailable'
        requests = state['requests'] if state else {'current': {}, 'prior': {}}
        content = (f'Cached SPY setup ({VERSION}); no provider request made.\n'
                   f'Source: ThetaData SPY 1m | latest minute start: {display_time(latest, include_date=True)} | age from close: {age}\n'
                   f'Collection: {"on" if collecting else "off"} | publication: {"on" if publishing else "off"} '
                   f'(requested: {"on" if requested else "off"}; blocked: {blocked}) '
                   f'| destination: {getattr(config, "SPX_CHANNEL_ID", 0)}\n'
                   f'Last poll: {display_time(state["last_poll_at"] if state else None, include_date=True)} | requests: {requests}\n'
                   f'Deliveries: {counts}. Counts are attempts, not billing units. Outcomes not evaluated.\n'
                   f'Informational range / BB (today): '
                   f'{self._informational_counts(now.astimezone(market_calendar.ET).date())}\n'
                   'BB: 1m + 5m, 20 periods / 2σ, session-only; first eligible crosses at '
                   '6:51 AM / 8:15 AM Pacific. Informational, not entries.')
        if state and state['observations']:
            recorded = state['observations'][-1]['snapshot']['classification']
            current = snapshot.get('classification', {})
            if (current.get('available') and current.get('bar_at') == recorded['bar_at']
                    and state_key(current) != state_key(recorded)):
                content += (f'\nHistory changed: embed is a recomputation, NOT a new alert. '
                            f'Recorded 1m start {display_time(recorded["bar_at"], include_date=True)}: '
                            f'{recorded["direction"]}, lifecycle={recorded["lifecycle"]}. '
                            f'Last acknowledged state: {state["last_acknowledged_state"]}.')
        await ctx.send(content=content, embed=snapshot_embed(snapshot))

    @tasks.loop(seconds=60)
    async def monitor(self):
        await self.poll_once()
        await self.daily_once()

    @monitor.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @monitor.error
    async def monitor_error(self, _exc):
        logger.warning('SPX collection loop interrupted; restarting in 30 seconds')
        await asyncio.sleep(30)
        self.monitor.restart()
