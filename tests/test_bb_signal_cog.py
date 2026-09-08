"""Offline collection/delivery integration; no paid requests or Discord calls."""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
import math
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from zero_dte.bb_signal_cog import BBSignalCog, collection_open
from zero_dte.bb_signals import VERSION, build_snapshot
from zero_dte.orb_audit import new_record, session_time, update_record

DAY = date(2026, 9, 8)
PRIOR = date(2026, 9, 4)
OLDER = date(2026, 9, 3)


def bars(day, count, spike=False):
    result = []
    for i in range(count):
        value = 120 if spike and i == count - 1 else 100 + math.sin(i * .8)
        stamp = session_time(day, 9, 30) + timedelta(minutes=i)
        result.append(dict(timestamp=stamp.isoformat(), available_at=(stamp + timedelta(minutes=1)).isoformat(),
                           open=value, high=value + .1, low=value - .1, close=value))
    return result


class BBSignalCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = session_time(DAY, 10, 11) + timedelta(seconds=2)
        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot = Mock()
        self.bot.get_channel.return_value = self.channel
        self.theta = Mock()
        self.theta._last_request_id = 'current-1'
        self.theta.index_history_ohlc.side_effect = self.provider
        self.calls = []
        self.current = bars(DAY, 41, spike=True)
        self.prior = bars(PRIOR, 390)
        self.older = bars(OLDER, 390)
        self.cog = self.make_cog()
        self.addCleanup(patch.stopall)
        patch('zero_dte.bb_signal_cog.config.SPX_CONTEXT_CHANNEL_ID', 123).start()
        patch('zero_dte.bb_signal_cog.audit_active', return_value=False).start()

    def make_cog(self):
        cog = BBSignalCog(self.bot, self.theta, directory=self.root / 'bb-events',
                          audit_directory=self.root / 'audit')
        cog._now = lambda: self.now
        return cog

    def provider(self, day, **kwargs):
        self.calls.append((day, kwargs))
        self.theta._last_request_id = 'current-1' if day == DAY else 'prior-1' if day == PRIOR else 'older-1'
        return deepcopy(self.current if day == DAY else self.prior if day == PRIOR else self.older)

    def state(self, count=41, spike=True):
        state = self.cog._load(DAY)
        state['record']['bars'] = bars(DAY, count, spike)
        state['bar_request_ids'] = {bar['timestamp']: 'current-1' for bar in state['record']['bars']}
        self.cog._state = state
        return state

    async def test_real_collection_event_only_and_gap_cutoff(self):
        await self.cog.poll_once()
        state = self.cog._state
        self.assertEqual(self.calls[0], (DAY, dict(symbol='SPX', interval='1m',
                                                 start_time='09:30:00', end_time='10:11:00')))
        self.assertEqual([day for day, _ in self.calls], [DAY, PRIOR, OLDER])
        self.assertEqual(state['version'], VERSION)
        self.assertEqual(state['requests']['current']['succeeded'], 1)
        self.assertTrue(state['prior_complete'])
        self.assertTrue(all(bar['available_at'] == self.now.isoformat() for bar in state['previous_bars']))
        self.assertEqual(self.channel.send.await_count, 1)
        entry = state['deliveries'][0]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['consumed_request_ids'], {'spx': ['current-1'], 'prior_spx': []})
        self.assertEqual(entry['outcome_status'], 'not_evaluated')
        await self.cog.poll_once()
        self.assertEqual(self.calls[-1][1]['start_time'], '10:10:00')
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(len(state['deliveries']), 1)
        # No transition and no heartbeat even after the old heartbeat interval.
        self.now += timedelta(minutes=31)
        await self.cog._assess(state)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertFalse(state['snapshot']['fresh'])

    async def test_two_session_cache_warms_first_15m_and_survives_restart(self):
        self.now = session_time(DAY, 9, 31) + timedelta(seconds=2)
        state = self.state(1, spike=False)
        await self.cog._prior(state, self.now)
        self.assertTrue(state['prior_complete'])
        self.assertEqual(state['requests']['prior']['started'], 2)
        restored = self.make_cog()._load(DAY)
        self.assertEqual(restored['prior_sessions'], state['prior_sessions'])
        await self.cog._prior(restored, self.now + timedelta(minutes=16))
        self.assertEqual(len(self.calls), 2)  # Complete days never refetched.
        state['record']['bars'] = bars(DAY, 15, spike=True)
        self.now = session_time(DAY, 9, 45) + timedelta(seconds=2)
        await self.cog._assess(state)
        self.assertTrue(state['snapshot']['timeframes']['15']['available'])
        self.assertEqual(state['snapshot']['prior_days'], [OLDER.isoformat(), PRIOR.isoformat()])
        self.assertEqual(self.cog._consumed(state, state['snapshot'])['prior_spx'], ['older-1', 'prior-1'])

    async def test_partial_older_history_has_no_consumption_credit_and_retries_independently(self):
        state = self.state()
        self.older = bars(OLDER, 389)
        await self.cog._prior(state, self.now)
        self.assertFalse(state['prior_complete'])
        self.assertTrue(state['prior_sessions'][PRIOR.isoformat()]['complete'])
        first = build_snapshot(state['record'], state['previous_bars'], self.now)
        self.assertEqual(first['prior_days'], [PRIOR.isoformat()])
        self.assertEqual(self.cog._consumed(state, first)['prior_spx'], ['prior-1'])
        await self.cog._prior(state, self.now)
        self.assertEqual(len(self.calls), 2)
        first_row = next(b for b in state['previous_bars'] if b['timestamp'] == self.older[0]['timestamp'])
        original_row = deepcopy(first_row)
        self.now += timedelta(minutes=15)
        self.older = bars(OLDER, 390)
        self.older[0].update(open=999, high=999, low=999, close=999)
        original_provider = self.provider
        def revised_provider(day, **kwargs):
            result = original_provider(day, **kwargs)
            self.theta._last_request_id = 'older-2'
            return result
        self.theta.index_history_ohlc.side_effect = revised_provider
        await self.cog._prior(state, self.now)
        self.assertEqual([d for d, _ in self.calls], [PRIOR, OLDER, OLDER])
        self.assertTrue(state['prior_complete'])
        retained = next(b for b in state['previous_bars'] if b['timestamp'] == original_row['timestamp'])
        self.assertEqual(retained, original_row)
        self.assertEqual(state['prior_bar_request_ids'][original_row['timestamp']], 'older-1')
        self.assertEqual(state['prior_bar_request_ids'][self.older[-1]['timestamp']], 'older-2')
        self.assertEqual(self.make_cog()._load(DAY)['previous_bars'], state['previous_bars'])

    async def test_cached_older_without_immediate_has_no_history_consumption(self):
        state = self.state()
        self.prior = []
        await self.cog._prior(state, self.now)
        snapshot = build_snapshot(state['record'], state['previous_bars'], self.now)
        self.assertEqual(snapshot['prior_days'], [])
        self.assertEqual(self.cog._consumed(state, snapshot)['prior_spx'], [])
        self.assertFalse(state['prior_complete'])
        self.assertTrue(state['prior_sessions'][OLDER.isoformat()]['complete'])

    async def test_prior_retry_deadlines_are_independent_and_persisted(self):
        state = self.state()
        original = self.provider
        def failed_recent(day, **kwargs):
            if day == PRIOR:
                self.calls.append((day, kwargs))
                raise RuntimeError('provider failure')
            return original(day, **kwargs)
        self.theta.index_history_ohlc.side_effect = failed_recent
        await self.cog._prior(state, self.now)
        restored = self.make_cog()._load(DAY)
        self.assertEqual(restored['requests']['prior'], {'started': 2, 'succeeded': 1, 'failed': 1})
        self.now += timedelta(minutes=14)
        await self.cog._prior(restored, self.now)
        self.assertEqual(len(self.calls), 2)
        self.now += timedelta(minutes=1)
        self.theta.index_history_ohlc.side_effect = original
        await self.cog._prior(restored, self.now)
        self.assertEqual([d for d, _ in self.calls], [PRIOR, OLDER, PRIOR])
        self.assertTrue(restored['prior_complete'])

    async def test_load_rejects_unexpected_prior_day_and_incorrect_completeness(self):
        state = self.state()
        await self.cog._prior(state, self.now)
        valid = deepcopy(state)
        state['previous_bars'] += bars(date(2026, 9, 2), 1)
        self.cog._save(state, self.now)
        with self.assertRaisesRegex(ValueError, 'Unexpected BB prior session'):
            self.make_cog()._load(DAY)
        valid['prior_sessions'][OLDER.isoformat()]['complete'] = False
        self.cog._save(valid, self.now)
        with self.assertRaisesRegex(ValueError, 'Invalid BB prior completeness'):
            self.make_cog()._load(DAY)

    async def test_prior_fetch_uses_each_sessions_actual_close(self):
        day = date(2026, 11, 30)
        recent, older = date(2026, 11, 27), date(2026, 11, 25)
        self.now = session_time(day, 9, 31)
        state = self.cog._load(day)
        self.theta.index_history_ohlc.side_effect = lambda d, **kw: bars(d, 210 if d == recent else 390)
        await self.cog._prior(state, self.now)
        calls = self.theta.index_history_ohlc.call_args_list
        self.assertEqual([(c.args[0], c.kwargs['end_time']) for c in calls],
                         [(recent, '13:00:00'), (older, '16:00:00')])
        self.assertTrue(state['prior_complete'])
        self.assertEqual(self.make_cog()._load(day)['previous_bars'], state['previous_bars'])

    async def test_warmup_and_no_event_do_not_post(self):
        state = self.state(1, spike=False)
        self.now = session_time(DAY, 9, 31) + timedelta(seconds=2)
        await self.cog._assess(state)
        self.assertTrue(state['snapshot']['fresh'])
        self.assertEqual(state['snapshot']['timeframes']['1']['reason'], 'warming_up')
        self.channel.send.assert_not_awaited()
        self.assertEqual(state['deliveries'], [])

    async def test_restart_dedup_and_pending_becomes_unknown(self):
        state = self.state()
        await self.cog._assess(state)
        restored = self.make_cog()._load(DAY)
        await self.cog._assess(restored)
        self.assertEqual(self.channel.send.await_count, 1)
        restored['deliveries'][0]['delivery'] = 'pending'
        self.cog._save(restored, self.now)
        restored = self.make_cog()._load(DAY)
        self.assertEqual(restored['deliveries'][0]['delivery'], 'unknown')
        self.assertEqual(restored['deliveries'][0]['reason'], 'interrupted_delivery')
        await self.cog._assess(restored)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_simultaneous_timeframes_one_message_all_events(self):
        self.now = session_time(DAY, 12) + timedelta(seconds=2)
        state = self.state(150)
        state['previous_bars'] = bars(PRIOR, 390)
        state['prior_bar_request_ids'] = {bar['timestamp']: 'prior-1' for bar in state['previous_bars']}
        expected = build_snapshot(state['record'], state['previous_bars'], self.now)
        self.assertEqual({event['timeframe'] for event in expected['events']}, {'1', '5', '15'})
        await self.cog._assess(state)
        entry = state['deliveries'][0]
        self.assertEqual(entry['event_ids'], [event['id'] for event in expected['events']])
        self.assertEqual(entry['consumed_request_ids']['prior_spx'], ['prior-1'])
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(len(self.channel.send.call_args.kwargs['embed'].fields), 3)

    async def test_skipped_boundary_recovers_fresh_higher_timeframes_once_across_restart(self):
        self.now = session_time(DAY, 12, 1) + timedelta(seconds=2)
        state = self.state(150)
        state['record']['bars'] += bars(DAY, 151)[-1:]
        state['previous_bars'] = bars(PRIOR, 390)
        state['prior_sessions'][PRIOR.isoformat()]['complete'] = True
        await self.cog._assess(state)
        self.assertEqual(self.channel.send.await_count, 1)
        events = state['deliveries'][0]['snapshot']['events']
        self.assertTrue({'5', '15'} <= {event['timeframe'] for event in events})
        restored = self.make_cog()._load(DAY)
        await self.cog._assess(restored)
        self.assertEqual(self.channel.send.await_count, 1)
        self.now = session_time(DAY, 12, 1) + timedelta(seconds=31)
        fresh = build_snapshot(state['record'], state['previous_bars'], self.now)
        self.assertTrue(fresh['fresh'])
        self.assertFalse(any(e['timeframe'] in ('5', '15') for e in fresh['events']))

    async def test_mixed_age_retry_suppresses_intact_group_when_one_event_expires(self):
        self.now = session_time(DAY, 12, 1) + timedelta(seconds=2)
        state = self.state(150)
        state['record']['bars'] += bars(DAY, 151)[-1:]
        state['previous_bars'] = bars(PRIOR, 390)
        state['prior_sessions'][PRIOR.isoformat()]['complete'] = True
        self.bot.get_channel.return_value = None
        await self.cog._assess(state)
        entry = state['deliveries'][0]
        original_ids = entry['event_ids'][:]
        self.assertEqual(entry['delivery'], 'failed')
        self.assertTrue({'1', '15'} <= {e['timeframe'] for e in entry['snapshot']['events']})
        self.now = session_time(DAY, 12, 1) + timedelta(seconds=31)
        self.assertTrue(self.cog._fresh(entry['snapshot'], self.now))
        self.bot.get_channel.return_value = self.channel
        await self.cog._assess(state)
        self.channel.send.assert_not_awaited()
        self.assertEqual(entry['delivery'], 'suppressed')
        self.assertEqual(entry['reason'], 'expired_before_send')
        self.assertEqual(entry['event_ids'], original_ids)
        self.assertEqual(self.make_cog()._load(DAY)['deliveries'][0]['event_ids'], original_ids)

    async def test_event_ages_checked_immediately_before_first_send_and_after_ack(self):
        self.now = session_time(DAY, 12, 1) + timedelta(seconds=2)
        state = self.state(150)
        state['record']['bars'] += bars(DAY, 151)[-1:]
        state['previous_bars'] = bars(PRIOR, 390)
        self.bot.get_channel.return_value = None
        await self.cog._assess(state)
        entry = state['deliveries'][0]
        self.bot.get_channel.return_value = self.channel
        async def delayed_ack(**kwargs):
            self.now = session_time(DAY, 12, 1) + timedelta(seconds=31)
        self.channel.send.side_effect = delayed_ack
        await self.cog._deliver(state, entry)
        self.assertTrue(self.cog._fresh(entry['snapshot'], self.now))
        self.assertEqual(entry['reason'], 'delivered_stale')
        self.channel.send.reset_mock()
        await self.cog._deliver(state, entry)
        self.channel.send.assert_not_awaited()
        self.assertEqual(entry['reason'], 'expired_before_send')

    async def test_missing_channel_fails_closed_and_does_not_use_other_channel(self):
        state = self.state()
        with patch('zero_dte.bb_signal_cog.config.SPX_CONTEXT_CHANNEL_ID', 0):
            await self.cog._assess(state)
        self.bot.get_channel.assert_not_called()
        self.channel.send.assert_not_awaited()
        self.assertEqual(state['deliveries'][0]['reason'], 'channel_disabled')
        await self.cog._assess(state)
        self.channel.send.assert_not_awaited()  # Do not revive suppressed backlog.

    async def test_confirmed_missing_channel_retries_while_fresh(self):
        state = self.state()
        self.bot.get_channel.return_value = None
        await self.cog._assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'failed')
        self.bot.get_channel.return_value = self.channel
        await self.cog._assess(state)
        self.channel.send.assert_awaited_once()
        self.assertEqual(len(state['deliveries']), 1)
        self.assertEqual(state['deliveries'][0]['delivery'], 'sent')

    async def test_definitive_discord_rejection_can_retry_but_timeout_cannot(self):
        state = self.state()
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'denied')
        await self.cog._assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'failed')
        self.channel.send.side_effect = TimeoutError()
        await self.cog._assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'unknown')
        self.channel.send.side_effect = None
        await self.cog._assess(state)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_expired_failed_event_is_not_retried(self):
        state = self.state()
        self.bot.get_channel.return_value = None
        await self.cog._assess(state)
        self.bot.get_channel.return_value = self.channel
        self.now += timedelta(seconds=91)
        await self.cog._assess(state)
        self.channel.send.assert_not_awaited()

    async def test_pre_send_persistence_failure_prevents_send(self):
        state = self.state()
        original = self.cog._save
        def fail_pending(s, now):
            if s['deliveries']:
                raise OSError('disk unavailable')
            return original(s, now)
        with patch.object(self.cog, '_save', side_effect=fail_pending):
            with self.assertRaises(OSError):
                await self.cog._assess(state)
        self.channel.send.assert_not_awaited()

    async def test_pending_intent_is_on_disk_before_send_and_stale_acknowledgement_is_marked(self):
        state = self.state()
        async def send(**kwargs):
            saved = json.loads(self.cog._path(DAY).read_text())
            self.assertEqual(saved['deliveries'][0]['delivery'], 'pending')
            self.assertEqual(saved['deliveries'][0]['attempts'], 1)
            self.now += timedelta(seconds=100)
        self.channel.send.side_effect = send
        await self.cog._assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'sent')
        self.assertEqual(state['deliveries'][0]['reason'], 'delivered_stale')

    async def test_stale_before_send_suppressed(self):
        state = self.state()
        await self.cog._assess(state)
        entry = state['deliveries'][0]
        self.channel.send.reset_mock()
        self.now += timedelta(seconds=100)
        await self.cog._deliver(state, entry)
        self.channel.send.assert_not_awaited()
        self.assertEqual(entry['reason'], 'expired_before_send')

    async def test_cached_private_status_never_fetches_and_expires(self):
        self.state()
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        await self.cog._cached(ctx)
        self.assertIn('no provider request', ctx.send.call_args.kwargs['content'])
        self.now += timedelta(minutes=3)
        await self.cog._cached(ctx)
        embed = ctx.send.call_args.kwargs['embed'].to_dict()
        self.assertIn('stale', json.dumps(embed).lower())
        self.assertNotIn('120.00', json.dumps(embed))
        ctx.channel.id = 456
        await self.cog._cached(ctx)
        self.assertEqual(ctx.send.call_args.args, ('BB indicators are available only in the configured private channel.',))
        self.theta.index_history_ohlc.assert_not_called()
        self.assertEqual(self.cog._state['deliveries'], [])

    async def test_audit_counts_are_bb_only_not_claimed_performance(self):
        state = self.state()
        await self.cog._assess(state)
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        await self.cog.cmd_audit.callback(self.cog, ctx)
        text = ctx.send.call_args.args[0]
        self.assertIn("'sent': 1", text)
        self.assertIn('BB outcomes: not evaluated', text)
        self.assertIn('no new flow collection', text)
        self.theta.index_history_ohlc.assert_not_called()
        ctx.channel.id = 456
        await self.cog.cmd_audit.callback(self.cog, ctx)
        self.assertEqual(ctx.send.call_args.args[0], 'Audit summaries are available only in the configured private channel.')

    async def test_cached_commands_remain_responsive_during_collection(self):
        self.state()
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        async with self.cog._lock:
            await asyncio.wait_for(self.cog._cached(ctx), timeout=1)
            await asyncio.wait_for(self.cog.cmd_audit.callback(self.cog, ctx), timeout=1)
        self.assertEqual(ctx.send.await_count, 2)
        self.theta.index_history_ohlc.assert_not_called()

    async def test_corrupt_ledger_is_not_reset_or_sent(self):
        state = self.state()
        await self.cog._assess(state)
        state['deliveries'][0]['event_ids'] = ['invented']
        self.cog._save(state, self.now)
        with self.assertRaises(ValueError):
            self.make_cog()._load(DAY)
        before = self.cog._path(DAY).read_bytes()
        new = self.make_cog()
        await new.poll_once()
        self.assertIsNone(new._state)
        self.assertEqual(before, self.cog._path(DAY).read_bytes())
        self.theta.index_history_ohlc.assert_not_called()

    def test_only_new_command_surface_and_no_legacy_imports(self):
        self.assertEqual(self.cog.qualified_name, 'BBSignals')
        self.assertEqual({command.name for command in self.cog.get_commands()}, {'bb', 'status', 'audit'})
        import inspect
        source = inspect.getsource(__import__('zero_dte.bb_signal_cog', fromlist=['']))
        for forbidden in ('spx_context', 'spx_actions', 'fetch_proxy', 'capture_flow', 'SPXFlowCog'):
            self.assertNotIn(forbidden, source)

    async def test_early_close_collects_final_bar_without_afterhours_post(self):
        early = date(2026, 11, 27)
        self.now = session_time(early, 13, 1)
        current = bars(early, 211, spike=True)  # Provider improperly includes a 13:00 bar.
        self.theta.index_history_ohlc.side_effect = lambda day, **kwargs: current if day == early else []
        await self.cog.poll_once()
        state = self.cog._state
        self.assertEqual(len(state['record']['bars']), 210)
        first = self.theta.index_history_ohlc.call_args_list[0]
        self.assertEqual(first.kwargs['end_time'], '13:00:00')
        self.channel.send.assert_not_awaited()
        self.assertFalse(collection_open(session_time(early, 13, 6)))
        self.assertFalse(collection_open(session_time(date(2026, 9, 7), 10)))

    async def test_current_saved_before_history_and_audit_mirror_is_versioned(self):
        original = self.provider
        def provider(day, **kwargs):
            if day == PRIOR:
                saved = json.loads(self.cog._path(DAY).read_text())
                self.assertEqual(len(saved['record']['bars']), 41)
                self.assertEqual(saved['deliveries'][0]['delivery'], 'sent')
            return original(day, **kwargs)
        self.theta.index_history_ohlc.side_effect = provider
        with patch('zero_dte.bb_signal_cog.audit_active', return_value=True):
            await self.cog.poll_once()
        self.assertTrue((self.root / 'audit' / f'orb-{DAY}.json').exists())
        self.assertTrue((self.root / 'audit' / f'bb-events-{DAY}.json').exists())
        self.assertFalse(list(self.root.rglob('context-*.json')))

    async def test_failures_back_off_and_prior_retries_are_bounded(self):
        self.theta.index_history_ohlc.side_effect = RuntimeError('not logged')
        for _ in range(4):
            await self.cog.poll_once()
        self.assertEqual(self.theta.index_history_ohlc.call_count, 5)  # Three current + two prior.
        self.assertEqual(self.cog._state['requests']['current']['failed'], 3)
        self.assertEqual(self.cog._state['requests']['prior']['failed'], 2)
        self.assertEqual(self.cog._pause_until, self.now + timedelta(minutes=5))
        self.channel.send.assert_not_awaited()

    async def test_singleflight_even_when_cancelled_thread_is_still_running(self):
        started, release = threading.Event(), threading.Event()
        def slow(day, **kwargs):
            started.set()
            release.wait(5)
            return []
        self.theta.index_history_ohlc.side_effect = slow
        first = asyncio.create_task(self.cog.poll_once())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            first.cancel()
            await asyncio.sleep(.02)
            self.assertTrue(self.cog._lock.locked())
            self.assertFalse(first.done())
            self.assertEqual(self.theta.index_history_ohlc.call_count, 1)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(self.cog._lock.locked())

    async def test_recovery_starts_at_earliest_gap_and_does_not_replay_backlog(self):
        state = self.state()
        state['record']['bars'].pop(10)
        self.cog._save(state, self.now)
        await self.cog.poll_once()
        self.assertEqual(self.calls[0][1]['start_time'], '09:40:00')
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertTrue(all(event['bar_at'] == self.current[-1]['timestamp']
                            for event in state['deliveries'][0]['snapshot']['events']))

    def test_existing_orb_preserved_on_same_day_migration_and_corrupt_not_overwritten(self):
        record = new_record(DAY)
        record['bars'] = bars(DAY, 20)
        record['status'] = 'waiting_opening_range'
        path = self.root / 'audit' / f'orb-{DAY}.json'
        self.cog._write(path, record)
        state = self.cog._load(DAY)
        self.assertEqual(state['record'], record)
        self.assertEqual(state['bar_request_ids'], {})  # Do not invent historical provenance.
        self.cog._write(path, {'schema': 999})
        with self.assertRaises(ValueError):
            self.cog._load(DAY)

    def legacy_context(self):
        record = new_record(DAY)
        update_record(record, bars(DAY, 20, spike=True),
                      session_time(DAY, 9, 50) + timedelta(seconds=2))
        self.assertEqual(record['candidate']['status'], 'prospective')
        self.assertIsNone(record['outcome'])
        return dict(schema=1, version='spx-context-v1', session_date=DAY.isoformat(),
                    record=record, bar_request_ids={bar['timestamp']: 'legacy-request'
                                                    for bar in record['bars']})

    async def test_context_only_migration_preserves_candidate_receipts_and_lineage_after_poll(self):
        legacy = self.legacy_context()
        path = self.root / 'context' / f'context-{DAY}.json'
        self.cog._write(path, legacy)
        before = path.read_bytes()
        loaded = self.cog._load(DAY)
        self.assertEqual(loaded['record'], legacy['record'])
        self.assertEqual(loaded['bar_request_ids'], legacy['bar_request_ids'])
        self.assertEqual(loaded['deliveries'], [])
        # Recovery returns revised earlier prices; first-seen evidence wins.
        self.current[0].update(open=999, high=1000, low=998, close=999)
        await self.cog.poll_once()
        state = self.cog._state
        self.assertEqual(state['record']['bars'][:20], legacy['record']['bars'])
        self.assertEqual(state['record']['candidate'], legacy['record']['candidate'])
        for stamp, rid in legacy['bar_request_ids'].items():
            self.assertEqual(state['bar_request_ids'][stamp], rid)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.make_cog()._load(DAY)['bar_request_ids'], state['bar_request_ids'])

    def test_context_and_mirror_must_be_consistent_and_valid(self):
        legacy = self.legacy_context()
        context_path = self.root / 'context' / f'context-{DAY}.json'
        mirror_path = self.root / 'audit' / f'orb-{DAY}.json'
        self.cog._write(context_path, legacy)
        self.cog._write(mirror_path, legacy['record'])
        self.assertEqual(self.cog._load(DAY)['record'], legacy['record'])
        self.assertEqual(self.cog._load(DAY)['bar_request_ids'], legacy['bar_request_ids'])
        mirror = deepcopy(legacy['record'])
        mirror['bars'][0]['available_at'] = session_time(DAY, 10).isoformat()
        self.cog._write(mirror_path, mirror)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.cog._load(DAY)
        self.cog._write(mirror_path, legacy['record'])
        self.cog._write(context_path, {'schema': 999})
        with self.assertRaises(ValueError):
            self.cog._load(DAY)
        self.cog._write(context_path, legacy)
        self.cog._write(mirror_path, {'schema': 999})
        with self.assertRaises(ValueError):
            self.cog._load(DAY)
        self.assertFalse(self.cog._path(DAY).exists())

    async def test_corrupt_context_lineage_fails_closed_without_requests_or_overwrite(self):
        legacy = self.legacy_context()
        legacy['bar_request_ids']['not-a-bar'] = 'wrong-request'
        path = self.root / 'context' / f'context-{DAY}.json'
        self.cog._write(path, legacy)
        before = path.read_bytes()
        await self.cog.poll_once()
        self.theta.index_history_ohlc.assert_not_called()
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.cog._state)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.cog._path(DAY).exists())

    async def test_complete_prior_retains_first_seen_lineage_across_partial_responses(self):
        state = self.cog._load(DAY)
        self.prior = bars(PRIOR, 390)[:200]
        await self.cog._prior(state, self.now)
        first_stamp = self.prior[0]['timestamp']
        first_receipt = next(b['available_at'] for b in state['previous_bars'] if b['timestamp'] == first_stamp)
        self.assertFalse(state['prior_complete'])
        self.now += timedelta(minutes=15)
        self.prior = bars(PRIOR, 390)
        await self.cog._prior(state, self.now)
        self.assertTrue(state['prior_complete'])
        self.assertEqual(next(b['available_at'] for b in state['previous_bars']
                              if b['timestamp'] == first_stamp), first_receipt)
        self.assertEqual(state['previous_bars'][-1]['available_at'], self.now.isoformat())
        self.assertEqual(state['requests']['prior']['started'], 3)
        self.assertEqual([d for d, _ in self.calls].count(OLDER), 1)
        self.assertEqual(self.make_cog()._load(DAY)['previous_bars'], state['previous_bars'])


if __name__ == '__main__':
    unittest.main()
