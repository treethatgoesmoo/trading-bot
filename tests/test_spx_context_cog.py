import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
from core.thetadata_client import IndexBar
from zero_dte.orb_audit import session_time
from zero_dte.spx_context_cog import SPXContextCog

DAY = date(2026, 9, 8)
def at(hour, minute=0, second=0):
    return session_time(DAY, hour, minute) + timedelta(seconds=second)

def bars():
    return [IndexBar(at(9, 30) + timedelta(minutes=i), 100, 101, 99, 100) for i in range(15)] + [IndexBar(at(9, 45), 100, 103, 100, 102)]


class ContextRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = MagicMock()
        self.bot.get_cog.return_value = None
        self.channel = MagicMock()
        self.channel.send = AsyncMock()
        self.bot.get_channel.return_value = self.channel
        self.theta = MagicMock()
        self.theta.index_history_ohlc.return_value = bars()
        self.theta._last_request_id = 'abc123'
        self.cog = SPXContextCog(self.bot, self.theta, directory=Path(self.temp.name) / 'context')
        self.cog.audit_directory = Path(self.temp.name) / 'audit'
        self.clock = at(9, 46, 10)
        for name, value in [('SPX_CONTEXT_CHANNEL_ID', 1), ('SPX_CONTEXT_PROXY_ENABLED', False),
                            ('THETA_AUDIT_START_DATE', DAY.isoformat()), ('THETA_AUDIT_DAYS', 30)]:
            p = patch.object(config, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(self.cog, '_now', side_effect=lambda: self.clock)
        p.start()
        self.addCleanup(p.stop)

    async def test_shared_current_collection_and_audit_links(self):
        await self.cog.poll_once()
        self.assertEqual(self.theta.index_history_ohlc.call_count, 2)  # current + prior, not duplicate audit
        self.assertEqual(self.theta.index_history_ohlc.call_args_list[0].args[0], DAY)
        state = self.cog._state
        entry = state['assessments'][0]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['request_ids']['spx'], 'abc123')
        self.assertEqual(len(state['bar_request_ids']), 16)
        self.assertTrue((self.cog.audit_directory / f'orb-{DAY}.json').exists())
        self.assertTrue((self.cog.audit_directory / f'context-{DAY}.json').exists())
        self.theta.option_trade_quotes.assert_not_called()
        self.channel.send.assert_awaited_once()

    async def test_suppressed_poll_and_restart_dedup(self):
        await self.cog.poll_once()
        other = SPXContextCog(self.bot, self.theta, directory=self.cog.directory)
        other.audit_directory = self.cog.audit_directory
        with patch.object(other, '_now', return_value=self.clock):
            await other.poll_once()
        self.assertEqual(other._state['assessments'][-1]['delivery'], 'suppressed')
        self.channel.send.assert_awaited_once()

    async def test_no_audit_after_expiry_but_live_continues(self):
        with patch('zero_dte.spx_context_cog.audit_active', return_value=False):
            await self.cog.poll_once()
        self.assertTrue(self.cog._path(DAY).exists())
        self.assertFalse(self.cog.audit_directory.exists())
        self.channel.send.assert_awaited_once()

    async def test_disabled_channel_and_cached_commands_no_network(self):
        with patch.object(config, 'SPX_CONTEXT_CHANNEL_ID', 0):
            await self.cog.poll_once()
        self.assertEqual(self.cog._state['assessments'][-1]['delivery'], 'suppressed')
        await self.cog.poll_once()  # now publish with the restored channel setting
        count = self.theta.index_history_ohlc.call_count
        ctx = MagicMock()
        ctx.channel.id = 1
        ctx.send = AsyncMock()
        self.clock = at(16, 30)
        await self.cog.cmd_spx.callback(self.cog, ctx)
        self.assertEqual(self.theta.index_history_ohlc.call_count, count)
        self.assertIn('Cached', ctx.send.call_args.kwargs['content'])
        fields = {field.name: field.value for field in ctx.send.call_args.kwargs['embed'].fields}
        self.assertIn('expired', fields['Structure'])
        self.assertIn('NO TRADE', fields['Action'])

    async def test_fresh_on_demand_snapshot_is_logged_without_new_requests(self):
        await self.cog.poll_once()
        await self.cog.poll_once()  # latest evaluated snapshot was suppressed
        calls = self.theta.index_history_ohlc.call_count
        self.clock = at(9, 46, 20)
        ctx = MagicMock()
        ctx.channel.id = 1
        ctx.send = AsyncMock()
        await self.cog.cmd_spx.callback(self.cog, ctx)
        entry = self.cog._state['assessments'][-1]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['suppression_reason'], 'on_demand')
        self.assertEqual(entry['delivered_at'], self.clock.isoformat())
        self.assertEqual(entry['assessment']['as_of'], self.clock.isoformat())
        self.assertEqual(entry['assessment']['action']['state'], 'wait_confirmation')
        self.assertEqual(self.theta.index_history_ohlc.call_count, calls)
        ctx.send.assert_awaited_once()

    async def test_candidate_on_demand_is_preserved_but_expired_view_is_no_trade(self):
        from test_spx_actions import candidate
        await self.cog.poll_once()
        self.cog._state['assessments'][-1]['assessment'] = candidate()
        calls = self.theta.index_history_ohlc.call_count
        self.clock = at(9, 55, 20)
        ctx = MagicMock()
        ctx.channel.id = 1
        ctx.send = AsyncMock()
        await self.cog.cmd_spx.callback(self.cog, ctx)
        entry = self.cog._state['assessments'][-1]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['assessment']['action']['state'], 'consider_calls')
        count = len(self.cog._state['assessments'])
        self.clock = at(9, 57)
        await self.cog.cmd_spx.callback(self.cog, ctx)
        self.assertIn('NO TRADE', ctx.send.call_args.kwargs['embed'].fields[0].value)
        self.assertEqual(len(self.cog._state['assessments']), count)
        self.assertEqual(self.theta.index_history_ohlc.call_count, calls)
        self.assertEqual(entry['assessment']['action']['state'], 'consider_calls')  # original immutable

    async def test_candidate_withdrawal_bypasses_cooldown(self):
        from test_spx_actions import candidate
        from zero_dte.spx_actions import build_action
        ctx = candidate()
        state = dict(session_date=str(DAY), last_published_at=at(9, 55, 10).isoformat(),
                     last_published_signature=list(ctx['signature']))
        ctx['momentum']['state'] = 'up_fading'
        ctx['action'] = build_action(ctx)
        ctx['signature'][-2] = ctx['action']['state']
        self.assertEqual(self.cog._reason(state, ctx, at(9, 55, 20)), 'state_change')

    async def test_reconciliation_does_not_rewrite_unchanged_archives(self):
        state = self.cog._load(DAY)
        self.cog._save(state, self.clock)
        before = self.cog._path(DAY).stat().st_mtime_ns
        with patch.object(self.cog, '_save') as save:
            self.cog._reconcile(DAY + timedelta(days=1), self.clock + timedelta(days=1))
        save.assert_not_called()
        self.assertEqual(self.cog._path(DAY).stat().st_mtime_ns, before)

    async def test_audit_command_private_read_only_no_provider_calls(self):
        ctx = MagicMock()
        ctx.channel.id = 1
        ctx.send = AsyncMock()
        with patch('report_theta_audit.build_report', return_value='safe summary') as report:
            await self.cog.cmd_audit.callback(self.cog, ctx)
        report.assert_called_once()
        self.theta.index_history_ohlc.assert_not_called()
        self.assertEqual(ctx.send.call_args.kwargs['file'].filename, 'spx-audit.txt')
        ctx.channel.id = 2
        with patch('report_theta_audit.build_report') as report:
            await self.cog.cmd_audit.callback(self.cog, ctx)
        report.assert_not_called()

    async def test_corrupt_state_failclosed_and_premarket_no_calls(self):
        self.clock = at(6)
        await self.cog.poll_once()
        self.theta.index_history_ohlc.assert_not_called()
        self.clock = at(9, 46)
        self.cog.directory.mkdir()
        self.cog._path(DAY).write_text('{broken')
        with self.assertLogs('zero_dte.spx_context_cog', level='WARNING'):
            await self.cog.poll_once()
        self.theta.index_history_ohlc.assert_not_called()
        self.assertEqual(self.cog._path(DAY).read_text(), '{broken')

    async def test_late_delivery_not_graded_as_timely(self):
        async def delayed(**kwargs):
            self.clock = at(9, 49)
        self.channel.send.side_effect = delayed
        await self.cog.poll_once()
        entry = self.cog._state['assessments'][0]
        self.assertEqual(entry['delivery'], 'sent')
        self.assertEqual(entry['outcomes']['15']['reason'], 'delivery_stale')
        self.assertEqual(entry['delivered_at'], self.clock.isoformat())

    async def test_failed_send_retries_and_pending_restart_unknown(self):
        self.channel.send.side_effect = RuntimeError('SECRET')
        with self.assertLogs('zero_dte.spx_context_cog', level='WARNING') as logs:
            await self.cog.poll_once()
        self.assertNotIn('SECRET', str(logs.output))
        self.channel.send.side_effect = None
        await self.cog.poll_once()
        self.assertEqual(self.cog._state['assessments'][-1]['delivery'], 'sent')
        state = self.cog._state
        state['assessments'][-1]['delivery'] = 'pending'
        self.cog._save(state, self.clock)
        recovered = self.cog._load(DAY)
        self.assertEqual(recovered['assessments'][-1]['delivery'], 'unknown')

    async def test_private_channel_guard_and_opening_collection_without_alert(self):
        self.clock = at(9, 40)
        await self.cog.poll_once()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.cog._state['assessments'][-1]['suppression_reason'], 'opening_range_forming')
        ctx = MagicMock()
        ctx.channel.id = 999
        ctx.send = AsyncMock()
        await self.cog.cmd_spx.callback(self.cog, ctx)
        self.assertNotIn('embed', ctx.send.call_args.kwargs)
        self.assertIn('private', ctx.send.call_args.args[0])

    async def test_final_missing_outcomes_never_replaced(self):
        await self.cog.poll_once()
        state = self.cog._state
        state['assessments'][0]['outcomes']['15'] = {'status': 'missing'}
        self.cog._grade(state, at(11))
        self.assertEqual(state['assessments'][0]['outcomes']['15'], {'status': 'missing'})

    async def test_failure_circuit_breaker_no_secret_logs(self):
        self.theta.index_history_ohlc.side_effect = RuntimeError('SECRET')
        with self.assertLogs('zero_dte.spx_context_cog', level='WARNING') as logs:
            for _ in range(4):
                await self.cog.poll_once()
        self.assertEqual(self.cog._state['fetch_failures'], 3)
        self.assertIsNotNone(self.cog._pause_until)
        self.assertNotIn('SECRET', str(logs.output))
