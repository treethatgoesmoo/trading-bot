"""Offline state/delivery integration with mocked transport and temporary evidence."""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from core.session_bars import session_time, utc
from core.state_store import StateStore
from market_state.cog import EvidenceError, MarketStateCog, collection_open
from market_state.signals import VERSION, build_snapshot
from test_state_store import DAY, PRIOR, OLDER, bars, classified_snapshot, legacy_bb, legacy_record, save_json


class MarketStateCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = session_time(DAY, 9, 50) + timedelta(seconds=2)
        self.channel = SimpleNamespace(send=AsyncMock())
        self.bot = Mock()
        self.bot.get_channel.return_value = self.channel
        self.theta = Mock()
        self.theta._last_request_id = 'current-1'
        self.calls = []
        self.rows = {DAY: bars(DAY, 20), PRIOR: bars(PRIOR, 390), OLDER: bars(OLDER, 390)}
        self.theta.index_history_ohlc.side_effect = self.provider
        self.flags = dict(COLLECTION_ENABLED=True, PUBLICATION_ENABLED=True, SPX_CHANNEL_ID=123,
                          SPX_POLL_SECONDS=60, THETADATA_API_KEY='offline-test-key')
        for key, value in self.flags.items():
            patcher = patch(f'market_state.cog.config.{key}', value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cog = self.make_cog()

    def make_cog(self, client=True):
        cog = MarketStateCog(self.bot, self.theta if client else None,
                             directory=self.root / 'market-state', audit_directory=self.root / 'audit')
        cog._now = lambda: self.now
        return cog

    def provider(self, day, **kwargs):
        self.calls.append((day, kwargs))
        self.theta._last_request_id = str(day)
        return deepcopy(self.rows.get(day, []))

    def state(self):
        return self.cog._load(DAY)

    async def assess(self, state, minute=20, direction='bullish', volatile=False, seconds=2):
        self.now = session_time(DAY, 9, 30) + timedelta(minutes=minute, seconds=seconds)
        state['record']['bars'] = bars(DAY, minute)
        snapshot = classified_snapshot(minute=minute, direction=direction, volatile=volatile, seconds=seconds)
        with patch('market_state.cog.build_snapshot', return_value=snapshot):
            await self.cog._assess(state)
        return snapshot

    async def test_bullish_unchanged_bearish_clear_and_restart_dedup(self):
        state = self.state()
        await self.assess(state)
        await self.assess(state)  # Poll duplicate.
        await self.assess(state, 25)  # New observation, same state: no message.
        await self.assess(state, 30, 'bearish')
        await self.assess(state, 35, 'neutral')
        await self.assess(state, 40, 'neutral')
        self.assertEqual(len(state['observations']), 5)
        self.assertEqual(len(state['deliveries']), 3)
        self.assertEqual(self.channel.send.await_count, 3)
        titles = [c.kwargs['embed'].title for c in self.channel.send.await_args_list]
        self.assertIn('BULLISH', titles[0])
        self.assertIn('BEARISH', titles[1])
        self.assertIn('bias cleared', titles[2])
        restored = self.make_cog()
        self.cog = restored
        state = restored._load(DAY)
        await self.assess(state, 40, 'neutral')
        self.assertEqual(self.channel.send.await_count, 3)
        self.assertEqual(state['last_acknowledged_state'], dict(direction='neutral', volatile=False))
        self.assertTrue(all(i['outcome_status'] == 'not_evaluated' for i in state['observations']))

    async def test_initial_neutral_is_silent_and_volatility_is_independent(self):
        state = self.state()
        await self.assess(state, 20, 'neutral')
        self.channel.send.assert_not_awaited()
        self.assertEqual(len(state['observations']), 1)
        await self.assess(state, 25, 'neutral', True)
        await self.assess(state, 30, 'bullish', True)
        await self.assess(state, 35, 'bullish', False)
        await self.assess(state, 40, 'neutral', False)
        titles = [c.kwargs['embed'].title for c in self.channel.send.await_args_list]
        self.assertEqual(len(titles), 4)
        self.assertIn('VOLATILE', titles[0])
        self.assertIn('BULLISH + VOLATILE', titles[1])
        self.assertNotIn('VOLATILE', titles[2])
        self.assertIn('bias cleared', titles[3])

    async def test_missing_or_stale_data_cannot_clear_bias(self):
        state = self.state()
        await self.assess(state)
        empty = build_snapshot(dict(session_date=DAY.isoformat(), bars=[]), [], self.now)
        with patch('market_state.cog.build_snapshot', return_value=empty):
            await self.cog._assess(state)
        self.assertEqual(state['last_observed_state']['direction'], 'bullish')
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(len(state['observations']), 1)

    async def test_older_classification_does_not_rewind_state(self):
        state = self.state()
        await self.assess(state, 25)
        earlier = classified_snapshot(minute=20, direction='bearish')
        with patch('market_state.cog.build_snapshot', return_value=earlier):
            await self.cog._assess(state)
        self.assertEqual(state['observed_bar'], session_time(DAY, 9, 50).isoformat())
        self.assertEqual(state['snapshot']['reason'], 'backward_decision')
        self.assertEqual(self.channel.send.await_count, 1)
        self.cog.store.load(DAY)

    async def test_skipped_boundary_poll_recovers_once_at_90s_not_91(self):
        state = self.state()
        await self.assess(state, seconds=90)
        self.assertEqual(self.channel.send.await_count, 1)
        await self.assess(state, seconds=90)
        self.assertEqual(self.channel.send.await_count, 1)
        # An unseen next candle outside its own window is not a retrospective alert.
        await self.assess(state, 25, 'bearish', seconds=91)
        self.assertEqual(len(state['observations']), 1)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_fresh_minute_cannot_extend_old_five_minute_decision(self):
        state = self.state()
        state['record']['bars'] = bars(DAY, 22)
        self.now = session_time(DAY, 9, 52)
        snapshot = classified_snapshot(seconds=120)
        snapshot['bar_at'] = session_time(DAY, 9, 51).isoformat()
        with patch('market_state.cog.build_snapshot', return_value=snapshot):
            await self.cog._assess(state)
        self.assertTrue(self.cog._fresh(snapshot, self.now))
        self.assertFalse(self.cog._decision_fresh(snapshot, self.now))
        self.assertEqual(state['observations'], [])
        self.channel.send.assert_not_awaited()

    async def test_disabled_collection_missing_key_or_client_make_zero_requests(self):
        for name, value in [('COLLECTION_ENABLED', False), ('THETADATA_API_KEY', '')]:
            with patch(f'market_state.cog.config.{name}', value):
                await self.cog.poll_once()
        await self.make_cog(client=False).poll_once()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'market-state').exists())

    async def test_disabled_publication_and_channel_zero_suppress_without_replay(self):
        state = self.state()
        with patch('market_state.cog.config.PUBLICATION_ENABLED', False):
            await self.assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'suppressed')
        await self.assess(state)
        await self.assess(state, 25)  # No replay just because publishing is enabled.
        self.channel.send.assert_not_awaited()
        with patch('market_state.cog.config.SPX_CHANNEL_ID', 0):
            await self.assess(state, 30, 'bearish')
        self.assertEqual(state['deliveries'][-1]['delivery'], 'suppressed')
        self.bot.get_channel.assert_not_called()

    async def test_confirmed_channel_failure_can_retry_same_fresh_observation(self):
        state = self.state()
        self.bot.get_channel.return_value = None
        await self.assess(state)
        self.assertEqual(state['deliveries'][0]['delivery'], 'failed')
        self.bot.get_channel.return_value = self.channel
        await self.assess(state, seconds=45)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(state['deliveries'][0]['delivery'], 'sent')
        self.assertEqual(len(state['observations']), 1)

    async def test_discord_rejection_retries_but_ambiguous_send_never_does(self):
        state = self.state()
        rejection = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'rejected')
        self.channel.send.side_effect = [rejection, None]
        await self.assess(state)
        await self.assess(state, seconds=30)
        self.assertEqual(state['deliveries'][0]['attempts'], 2)
        self.assertEqual(state['deliveries'][0]['delivery'], 'sent')
        self.channel.send.side_effect = TimeoutError('unacknowledged')
        await self.assess(state, 25, 'bearish')
        await self.assess(state, 25, 'bearish', seconds=30)
        self.assertEqual(state['deliveries'][1]['delivery'], 'unknown')
        self.assertEqual(self.channel.send.await_count, 3)
        self.cog = self.make_cog()
        state = self.cog._load(DAY)
        await self.assess(state, 25, 'bearish', seconds=45)
        self.assertEqual(self.channel.send.await_count, 3)

    async def test_failed_old_state_suppressed_before_opposite_send(self):
        state = self.state()
        self.bot.get_channel.return_value = None
        await self.assess(state)
        self.bot.get_channel.return_value = self.channel
        await self.assess(state, 25, 'bearish')
        self.assertEqual(state['deliveries'][0]['delivery'], 'suppressed')
        self.assertEqual(state['deliveries'][1]['delivery'], 'sent')
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIn('BEARISH', self.channel.send.await_args.kwargs['embed'].title)

    async def test_same_candle_changed_inputs_do_not_resend_or_retry_original(self):
        state = self.state()
        self.bot.get_channel.return_value = None
        await self.assess(state)
        self.bot.get_channel.return_value = self.channel
        await self.assess(state, 20, 'bearish', seconds=30)
        self.channel.send.assert_not_awaited()
        self.assertEqual(len(state['observations']), 1)
        self.assertEqual(state['last_observed_state']['direction'], 'bullish')
        self.assertEqual(state['deliveries'][0]['reason'], 'superseded_or_unavailable')

    async def test_acknowledgement_after_expiry_is_sent_but_marked_stale(self):
        state = self.state()
        async def slow_send(**kwargs):
            self.now += timedelta(seconds=90)
        self.channel.send.side_effect = slow_send
        await self.assess(state)
        entry = state['deliveries'][0]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['reason'], 'delivered_stale')
        self.cog.store.load(DAY)

    async def test_slow_intent_persistence_cannot_authorize_expired_send(self):
        state = self.state()
        original = self.cog.store.save
        def slow_save(value):
            original(value)
            if value['deliveries'] and value['deliveries'][0]['delivery'] == 'pending' and value['deliveries'][0]['attempts']:
                self.now += timedelta(seconds=100)
        with patch.object(self.cog.store, 'save', side_effect=slow_save):
            await self.assess(state)
        self.channel.send.assert_not_awaited()
        self.assertEqual(state['deliveries'][0]['reason'], 'expired_before_send')

    async def test_publication_disabled_during_intent_save_prevents_send(self):
        import config
        state = self.state()
        original = self.cog.store.save
        def changed_config(value):
            original(value)
            if value['deliveries'] and value['deliveries'][0]['attempts']:
                config.PUBLICATION_ENABLED = False
        with patch.object(self.cog.store, 'save', side_effect=changed_config):
            await self.assess(state)
        self.channel.send.assert_not_awaited()
        self.assertEqual(state['deliveries'][0]['reason'], 'publication_disabled')

    async def test_save_failure_before_intent_blocks_in_memory_continuation(self):
        state = self.state()
        self.cog._save(state)
        original = self.cog.store.save
        def fail_intent(value):
            if value['deliveries'] and value['deliveries'][0]['attempts']:
                raise OSError('full disk')
            original(value)
        with patch.object(self.cog.store, 'save', side_effect=fail_intent):
            with self.assertRaises(EvidenceError):
                await self.assess(state)
        self.channel.send.assert_not_awaited()
        self.assertIsNone(self.cog._state)
        with self.assertRaises(EvidenceError):
            await self.cog._assess(state)
        restored = self.cog._load(DAY)
        await self.assess(restored)
        self.channel.send.assert_not_awaited()  # Unattempted persisted intent is not replayed.

    async def test_ack_save_failure_recovers_as_unknown_without_duplicate(self):
        state = self.state()
        original = self.cog.store.save
        def fail_ack(value):
            if value['deliveries'] and value['deliveries'][0]['delivery'] == 'sent':
                raise OSError('ack write failed')
            original(value)
        with patch.object(self.cog.store, 'save', side_effect=fail_ack):
            with self.assertRaises(EvidenceError):
                await self.assess(state)
        self.assertEqual(self.channel.send.await_count, 1)
        restored = self.cog._load(DAY)
        self.assertEqual(restored['deliveries'][0]['delivery'], 'unknown')
        await self.assess(restored)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_send_cancellation_is_unknown_even_without_restart(self):
        state = self.state()
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.channel.send.side_effect = blocked
        task = asyncio.create_task(self.assess(state))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state['deliveries'][0]['delivery'], 'unknown')
        self.channel.send.side_effect = None
        await self.assess(state)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.cog.store.load(DAY)['deliveries'][0]['delivery'], 'unknown')

    async def test_real_engine_collector_prior_warmup_then_directional_alert(self):
        values = [5000 + .0001 * i * i for i in range(800)]
        for day, seq in [(OLDER, values[:390]), (PRIOR, values[390:780]), (DAY, values[780:])]:
            rows = bars(day, len(seq))
            for row, price in zip(rows, seq):
                row.update(open=price, high=price + .1, low=price - .1, close=price)
            self.rows[day] = rows
        await self.cog.poll_once()
        state = self.cog._state
        self.assertEqual([d for d, _ in self.calls], [DAY, PRIOR, OLDER])
        self.assertEqual(self.calls[0][1]['end_time'], '09:50:00')
        self.assertTrue(state['prior_complete'])
        self.assertEqual(state['snapshot']['classification']['direction'], 'bullish')
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(state['observations'][0]['consumed_request_ids'],
                         dict(spx=[str(DAY)], prior_spx=sorted([str(PRIOR), str(OLDER)])))
        self.assertFalse({'candidate', 'outcome', 'rule'} & set(state['record']))
        self.assertFalse((self.root / 'audit').exists())
        await self.cog.poll_once()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[-1][1]['start_time'], '09:49:00')
        self.assertEqual(self.channel.send.await_count, 1)
        self.cog.store.load(DAY)

    async def test_prior_cache_first_seen_and_independent_retries_survive_restart(self):
        state = self.state()
        self.rows[OLDER] = bars(OLDER, 389)
        await self.cog._prior(state)
        self.assertFalse(state['prior_complete'])
        self.assertTrue(state['prior_sessions'][str(PRIOR)]['complete'])
        first = deepcopy(state['previous_bars'][0])
        restored_cog = self.make_cog()
        state = restored_cog._load(DAY)
        await restored_cog._prior(state)
        self.assertEqual(len(self.calls), 2)
        self.now += timedelta(minutes=15)
        self.rows[OLDER] = bars(OLDER, 390, price=999)
        await restored_cog._prior(state)
        self.assertEqual([d for d, _ in self.calls], [PRIOR, OLDER, OLDER])
        self.assertTrue(state['prior_complete'])
        self.assertEqual(state['previous_bars'][0], first)
        self.assertEqual(state['prior_bar_request_ids'][first['timestamp']], str(OLDER))
        self.assertEqual(state['previous_bars'][389]['close'], 999)

    async def test_no_prior_consumption_when_immediate_session_missing(self):
        state = self.state()
        self.rows[PRIOR] = []
        await self.cog._prior(state)
        state['record']['bars'] = bars(DAY, 20)
        snapshot = build_snapshot(state['record'], state['previous_bars'], self.now)
        self.assertEqual(snapshot['prior_days'], [])
        self.assertEqual(self.cog._consumed(state, snapshot)['prior_spx'], [])

    async def test_current_evidence_is_saved_before_slow_history(self):
        gate = asyncio.Event()
        entered = asyncio.Event()
        async def blocked_prior(state, now=None):
            on_disk = self.cog.store.load(DAY)
            self.assertEqual(len(on_disk['record']['bars']), 20)
            self.assertEqual(on_disk['requests']['current']['succeeded'], 1)
            entered.set()
            await gate.wait()
        with patch.object(self.cog, '_prior', side_effect=blocked_prior):
            task = asyncio.create_task(self.cog.poll_once())
            await entered.wait()
            gate.set()
            await task

    async def test_current_request_backoff_and_gap_cutoff(self):
        self.theta.index_history_ohlc.side_effect = RuntimeError('provider unavailable')
        for _ in range(3):
            await self.cog.poll_once()
        before = self.theta.index_history_ohlc.call_count
        await self.cog.poll_once()
        self.assertEqual(self.theta.index_history_ohlc.call_count, before)
        self.assertEqual(self.cog._state['requests']['current']['failed'], 3)
        self.now += timedelta(minutes=5)
        self.theta.index_history_ohlc.side_effect = self.provider
        await self.cog.poll_once()
        self.assertEqual(self.cog._state['requests']['current']['succeeded'], 1)
        self.assertIsNone(self.cog._pause_until)
        # Remove a minute: collection explicitly requests from that gap.
        self.cog._state['record']['bars'].pop(3)
        self.cog._state['bar_request_ids'].pop(session_time(DAY, 9, 33).isoformat())
        await self.cog.poll_once()
        self.assertEqual(self.calls[-1][1]['start_time'], '09:33:00')

    async def test_corrupt_evidence_blocks_requests_writes_and_publication(self):
        path = self.cog._path(DAY)
        path.parent.mkdir()
        path.write_text('{corrupt evidence')
        await self.cog.poll_once()
        self.assertEqual(self.calls, [])
        self.channel.send.assert_not_awaited()
        self.assertTrue(self.cog._persistence_fault)
        self.assertEqual(path.read_text(), '{corrupt evidence')

    async def test_corrupt_migration_blocks_collection_without_overwriting_source(self):
        path = self.root / 'bb-events' / f'bb-events-{DAY}.json'
        legacy = legacy_bb()
        legacy['bar_request_ids']['unknown'] = 'bad-id'
        original = save_json(path, legacy)
        await self.cog.poll_once()
        self.assertEqual(self.calls, [])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.cog._path(DAY).exists())

    async def test_context_migration_keeps_receipts_lineage_and_never_grades_orb(self):
        record = legacy_record()
        record['candidate'] = dict(status='prospective', direction='UP',
                                   available_at=session_time(DAY, 9, 46).isoformat(),
                                   signal_bar=session_time(DAY, 9, 45).isoformat(),
                                   entry_time=session_time(DAY, 9, 47).isoformat())
        path = self.root / 'context' / f'context-{DAY}.json'
        source = dict(schema=1, version='spx-context-v1', session_date=str(DAY), record=record,
                      bar_request_ids={b['timestamp']: 'original' for b in record['bars']})
        original = save_json(path, source)
        self.rows[DAY] = bars(DAY, 20, price=999)
        await self.cog.poll_once()
        state = self.cog._state
        self.assertEqual(state['record']['bars'], record['bars'])
        self.assertEqual(set(state['bar_request_ids'].values()), {'original'})
        self.assertFalse({'candidate', 'outcome'} & set(state['record']))
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'audit').exists())

    async def test_status_is_private_cached_and_does_not_wait_for_collection_lock(self):
        state = self.state()
        await self.assess(state)
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        await self.cog._lock.acquire()
        try:
            await asyncio.wait_for(MarketStateCog.cmd_status.callback(self.cog, ctx), timeout=.5)
        finally:
            self.cog._lock.release()
        self.assertEqual(self.calls, [])
        self.assertIn('no provider request', ctx.send.await_args.kwargs['content'])
        self.assertIn('publication: on', ctx.send.await_args.kwargs['content'])
        self.assertEqual(len(state['observations']), 1)
        other = SimpleNamespace(channel=SimpleNamespace(id=456), send=AsyncMock())
        await MarketStateCog.cmd_status.callback(self.cog, other)
        self.assertNotIn('embed', other.send.await_args.kwargs)
        self.assertNotIn('SPX 1m', other.send.await_args.args[0])
        self.assertEqual([c.name for c in self.cog.get_commands()], ['status'])
        self.assertEqual(self.cog.get_commands()[0].aliases, ['bb'])

    async def test_status_remains_available_without_collector_or_collected_session(self):
        cog = self.make_cog(client=False)
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        await MarketStateCog.cmd_status.callback(cog, ctx)
        self.assertIn('Collection: off', ctx.send.await_args.kwargs['content'])
        self.assertIn('unavailable', ctx.send.await_args.kwargs['embed'].title)
        self.assertEqual(self.calls, [])

    async def test_cancellation_keeps_single_flight_until_thread_finishes(self):
        entered, release = threading.Event(), threading.Event()
        active = []
        def blocked(day, **kwargs):
            active.append(day)
            entered.set()
            release.wait(5)
            return []
        self.theta.index_history_ohlc.side_effect = blocked
        first = asyncio.create_task(self.cog.poll_once())
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        first.cancel()
        second = asyncio.create_task(self.cog.poll_once())
        try:
            await asyncio.sleep(.03)
            first.cancel()  # Repeated cancellation must not release the lock.
            await asyncio.sleep(.03)
            self.assertTrue(self.cog._lock.locked())
            self.assertEqual(len(active), 1)
            with patch('market_state.cog.config.COLLECTION_ENABLED', False):
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                await second
        finally:
            release.set()
        self.assertEqual(len(active), 1)
        self.assertFalse(self.cog._lock.locked())
        stored = self.cog.store.load(DAY)
        self.assertEqual(stored['requests']['current'], dict(started=1, succeeded=0, failed=0))

    async def test_lifecycle_refuses_second_owner_and_releases_after_worker(self):
        from core.process_lock import ProcessLock
        entered, release = threading.Event(), threading.Event()
        self.bot.wait_until_ready = AsyncMock()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return []
        self.theta.index_history_ohlc.side_effect = blocked
        await self.cog.cog_load()
        unload = None
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            second = self.make_cog()
            with self.assertRaisesRegex(RuntimeError, 'Another service'):
                await second.cog_load()
            self.assertIsNone(second.monitor.get_task())
            self.channel.send.assert_not_awaited()
            self.assertEqual(self.theta.index_history_ohlc.call_count, 1)
            unload = asyncio.create_task(self.cog.cog_unload())
            await asyncio.sleep(.03)
            self.assertFalse(unload.done())
            with self.assertRaisesRegex(RuntimeError, 'Another service'):
                ProcessLock(self.cog.directory).acquire()
        finally:
            release.set()
            if unload is None:
                await self.cog.cog_unload()
            else:
                await asyncio.wait_for(unload, 3)
        with ProcessLock(self.cog.directory):
            pass

    async def test_start_failure_releases_ownership(self):
        from core.process_lock import ProcessLock
        with patch.object(self.cog.monitor, 'start', side_effect=RuntimeError('test failure')):
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                await self.cog.cog_load()
        with ProcessLock(self.cog.directory):
            pass

    async def test_status_distinguishes_requested_and_effective_publication(self):
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        with patch('market_state.cog.config.COLLECTION_ENABLED', False):
            await MarketStateCog.cmd_status.callback(self.cog, ctx)
        text = ctx.send.await_args.kwargs['content']
        self.assertIn('publication: off (requested: on; blocked: collection flag off)', text)
        self.theta.index_history_ohlc.assert_not_called()

    async def test_status_explains_same_candle_recomputation(self):
        state = self.state()
        await self.assess(state)
        changed = classified_snapshot(minute=20, direction='bearish')
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
        with patch('market_state.cog.build_snapshot', return_value=changed):
            await MarketStateCog.cmd_status.callback(self.cog, ctx)
        text = ctx.send.await_args.kwargs['content']
        self.assertIn('NOT a new alert', text)
        self.assertIn('bullish, volatile=False', text)
        self.assertIn('Last acknowledged state', text)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_early_close_prior_requests_and_closed_market_do_not_alert(self):
        day = date(2026, 11, 30)
        recent, older = date(2026, 11, 27), date(2026, 11, 25)
        self.now = session_time(day, 9, 31)
        self.rows.update({recent: bars(recent, 210), older: bars(older, 390)})
        state = self.cog._load(day)
        await self.cog._prior(state)
        self.assertEqual([(d, kw['end_time']) for d, kw in self.calls], [(recent, '13:00:00'), (older, '16:00:00')])
        self.assertTrue(state['prior_complete'])
        before = len(self.calls)
        self.now = session_time(day, 16, 6)
        await self.cog.poll_once()
        self.assertEqual(len(self.calls), before)
        self.channel.send.assert_not_awaited()


@pytest.mark.parametrize('day,hour,minute,expected', [
    (DAY, 9, 30, False), (DAY, 9, 31, True), (DAY, 16, 5, True), (DAY, 16, 6, False),
    (date(2026, 9, 7), 10, 0, False), (date(2026, 11, 27), 13, 5, True),
    (date(2026, 11, 27), 13, 6, False),
])
def test_collection_calendar(day, hour, minute, expected):
    assert collection_open(session_time(day, hour, minute)) is expected
