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
    async def assemble(self, start, *, full_flow=False, key='fake', audit_flow=True):
        import bot.core as core
        bot = MagicMock()
        bot.__aenter__ = AsyncMock(return_value=bot)
        bot.__aexit__ = AsyncMock(return_value=False)
        bot.add_cog = AsyncMock()
        bot.start = AsyncMock()
        client = MagicMock()
        client.close = AsyncMock()
        values = dict(DISCORD_TOKEN='fake', ZERO_DTE_ENABLED=False, SWING_ENABLED=False,
                      SPX_FLOW_ENABLED=full_flow, THETADATA_API_KEY=key,
                      THETA_AUDIT_START_DATE=start, THETA_AUDIT_DAYS=30,
                      THETA_AUDIT_FLOW_ENABLED=audit_flow, NOTIFICATION_MODE='legacy')
        with patch.multiple(core, **values), patch.object(core, 'build_bot', return_value=bot), \
                patch.object(core, 'MarketDataClient', return_value=client), \
                patch('core.theta_usage.audit_active', return_value=True):
            await core.run_bot()
        return [c.args[0] for c in bot.add_cog.call_args_list]

    async def test_default_off(self):
        cogs = await self.assemble('')
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog'])

    async def test_audit_installs_quiet_flow_not_full_watch(self):
        cogs = await self.assemble('2026-09-08')
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'SPXFlowCog', 'AuditCog'])
        self.assertTrue(cogs[1].audit_only)
        self.assertEqual(cogs[1].theta_client._purpose, 'audit_flow')
        self.assertEqual(cogs[2].theta_client._purpose, 'audit')

    async def test_existing_flow_is_not_duplicated(self):
        cogs = await self.assemble('2026-09-08', full_flow=True)
        flows = [c for c in cogs if isinstance(c, SPXFlowCog)]
        self.assertEqual(len(flows), 1)
        self.assertFalse(flows[0].audit_only)
        self.assertEqual(flows[0].theta_client._purpose, 'spx_flow')

    async def test_no_key_or_invalid_date_no_sampler(self):
        for start, key in [('2026-09-08', ''), ('not-a-date', 'fake')]:
            cogs = await self.assemble(start, key=key)
            self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog'])

    async def test_price_only_opt_out(self):
        cogs = await self.assemble('2026-09-08', audit_flow=False)
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'AuditCog'])
