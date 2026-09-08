"""Offline integration: opt-in audit never enables trade/contract alerts."""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from zero_dte.flow_cog import SPXFlowCog


class QuietFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.market = MagicMock()
        self.theta = MagicMock()
        self.theta.option_trade_quotes.return_value = []
        self.cog = SPXFlowCog(self.bot, self.market, self.theta, audit_only=True)
        self.now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)  # 10 ET

    async def test_quiet_morning_only_requests_flow(self):
        from core.market_calendar import ET
        now = self.now.astimezone(ET)
        with patch('zero_dte.flow_cog.audit_active', return_value=True):
            await self.cog._audit_flow_poll(now)
        self.theta.option_trade_quotes.assert_called_once()
        self.theta.index_snapshots.assert_not_called()
        self.theta.index_history_ohlc.assert_not_called()
        self.theta.option_chain_snapshot.assert_not_called()
        self.market.fetch_candles.assert_not_called()
        self.bot.get_channel.assert_not_called()
        self.assertEqual(self.cog._last_sample_at, now)
        self.assertEqual(self.cog._last_flow.total_trades, 0)

    async def test_expired_or_afternoon_no_calls(self):
        from core.market_calendar import ET
        now = self.now.astimezone(ET)
        with patch('zero_dte.flow_cog.audit_active', return_value=False):
            await self.cog._audit_flow_poll(now)
        with patch('zero_dte.flow_cog.audit_active', return_value=True):
            await self.cog._audit_flow_poll(now.replace(hour=11))
        self.theta.option_trade_quotes.assert_not_called()

    async def test_failure_preserves_old_timestamp_and_circuit_breaks(self):
        from core.market_calendar import ET
        now = self.now.astimezone(ET)
        self.theta.option_trade_quotes.side_effect = RuntimeError('PRIVATE-KEY')
        with patch('zero_dte.flow_cog.audit_active', return_value=True), self.assertLogs(
                'zero_dte.flow_cog', level='WARNING') as logs:
            for _ in range(4):
                await self.cog._audit_flow_poll(now)
        self.assertEqual(self.theta.option_trade_quotes.call_count, 3)
        self.assertIsNone(self.cog._last_sample_at)
        self.assertNotIn('PRIVATE-KEY', str(logs.output))

    async def test_command_does_not_expose_experimental_trade(self):
        ctx = MagicMock()
        ctx.send = AsyncMock()
        await self.cog.cmd_spxflow.callback(self.cog, ctx)
        self.assertIn('Local-only', ctx.send.call_args.args[0])
        self.theta.option_trade_quotes.assert_not_called()


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def assemble(self, start, *, key='fake'):
        import bot.core as core
        bot = MagicMock()
        bot.__aenter__ = AsyncMock(return_value=bot)
        bot.__aexit__ = AsyncMock(return_value=False)
        bot.add_cog = AsyncMock()
        bot.start = AsyncMock()
        values = dict(DISCORD_TOKEN='fake', THETADATA_API_KEY=key, SPX_CONTEXT_CHANNEL_ID=0)
        with patch.multiple(core, **values), patch.object(core, 'build_bot', return_value=bot), \
                patch('config.THETA_AUDIT_START_DATE', start), \
                patch('core.theta_usage.audit_active', return_value=True):
            await core.run_bot()
        return [c.args[0] for c in bot.add_cog.call_args_list]

    async def test_real_bb_cog_is_the_only_collector_with_or_without_audit(self):
        for start in ('', '2026-09-08', 'not-a-date'):
            with self.subTest(start=start):
                cogs = await self.assemble(start)
                self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'BBSignalCog'])
                self.assertEqual(cogs[1].theta_client._purpose, 'bb_events')
                self.assertEqual({command.name for command in cogs[1].get_commands()},
                                 {'bb', 'status', 'audit'})

    async def test_no_key_never_installs_fallback_or_flow_sampler(self):
        cogs = await self.assemble('2026-09-08', key='')
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog'])
