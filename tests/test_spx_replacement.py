"""Replacement-mode safety and notification/subscription integration."""
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from zero_dte.flow_cog import SPXFlowCog
from zero_dte.orb_audit import session_time


class ReplacementAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def assemble(self, *, mode='spx', key='fake', flow=True):
        import bot.core as core
        bot = MagicMock()
        bot.__aenter__ = AsyncMock(return_value=bot)
        bot.__aexit__ = AsyncMock(return_value=False)
        bot.add_cog = AsyncMock()
        bot.start = AsyncMock()
        client = MagicMock()
        client.close = AsyncMock()
        values = dict(DISCORD_TOKEN='fake', NOTIFICATION_MODE=mode,
                      ZERO_DTE_ENABLED=True, SWING_ENABLED=True, SPX_FLOW_ENABLED=True,
                      THETADATA_API_KEY=key, THETA_AUDIT_START_DATE='2026-09-08',
                      SPX_CONTEXT_FLOW_ENABLED=flow, SPX_CONTEXT_CHANNEL_ID=123)
        with patch.multiple(core, **values), patch.object(core, 'build_bot', return_value=bot), \
                patch.object(core, 'MarketDataClient', return_value=client):
            await core.run_bot()
        return bot, [call.args[0] for call in bot.add_cog.call_args_list]

    async def test_replaces_every_old_stream_and_shares_audit_collector(self):
        _, cogs = await self.assemble()
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'SPXFlowCog', 'SPXContextCog'])
        self.assertTrue(cogs[1].audit_only)
        self.assertTrue(cogs[1].context_session)
        self.assertEqual(cogs[1].theta_client._purpose, 'spx_context_flow')
        self.assertEqual(cogs[2].theta_client._purpose, 'spx_context')

    async def test_missing_key_never_falls_back_to_old_notifications(self):
        _, cogs = await self.assemble(key='')
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog'])

    async def test_options_opt_out_keeps_index_context(self):
        _, cogs = await self.assemble(flow=False)
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'SPXContextCog'])

    async def test_invalid_mode_fails_closed(self):
        bot, cogs = await self.assemble(mode='typo')
        self.assertEqual(cogs, [])
        bot.start.assert_not_awaited()

    async def test_shared_automatic_posts_and_tuning_suppressed(self):
        from bot.shared_cog import SharedCog
        cog = SharedCog(MagicMock(), MagicMock())
        channel = MagicMock()
        channel.send = AsyncMock()
        with patch('bot.shared_cog.NOTIFICATION_MODE', 'spx'), \
                patch('bot.shared_cog.fetch_events', new_callable=AsyncMock) as events:
            await cog.on_ready()
            await cog.maybe_post_econ_briefing(channel, '2026-09-08')
            await cog.maybe_tune_weights(channel, '2026-09-08')
        channel.send.assert_not_awaited()
        events.assert_not_awaited()
        cog.bot.get_channel.assert_not_called()
        self.assertIsNone(cog._tune_date)


class ContextFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_session_cache_is_quiet_and_audit_bounded(self):
        bot, market, theta = MagicMock(), MagicMock(), MagicMock()
        theta.option_trade_quotes.return_value = []
        theta._last_request_id = 'req-current'
        cog = SPXFlowCog(bot, market, theta, audit_only=True, context_session=True)
        now = session_time(date(2026, 9, 8), 14)
        with patch('zero_dte.flow_cog.audit_active', return_value=True):
            await cog._audit_flow_poll(now)
        theta.option_trade_quotes.assert_called_once()
        theta.index_history_ohlc.assert_not_called()
        theta.option_chain_snapshot.assert_not_called()
        market.fetch_candles.assert_not_called()
        bot.get_channel.assert_not_called()
        self.assertEqual(cog._last_flow_request_id, 'req-current')
        self.assertEqual(cog._last_flow_request_ids, [])  # no payload consumed from an empty result
        with patch('zero_dte.flow_cog.audit_active', return_value=False):
            await cog._audit_flow_poll(now + timedelta(minutes=1))
        self.assertEqual(theta.option_trade_quotes.call_count, 1)


class RequestLinkTests(unittest.TestCase):
    def test_request_link_matches_persisted_ticket_and_clears_outside_window(self):
        from core.thetadata_client import ThetaDataClient
        from core.theta_usage import RequestTicket
        sdk = MagicMock()
        sdk.index_history_ohlc.return_value = []
        theta = ThetaDataClient(client=sdk, purpose='spx_context')
        ticket = RequestTicket('id123', 'index_history_ohlc', 'index', 'spx_context',
                               '2026-09-08T14:00:00+00:00', '2026-09-08', 0)
        with patch('core.theta_usage.begin_request', return_value=ticket), \
                patch('core.theta_usage.finish_request'):
            theta._request('index_history_ohlc')
        self.assertEqual(theta._last_request_id, ticket.request_id)
        with patch('core.theta_usage.begin_request', return_value=None):
            theta._request('index_history_ohlc')
        self.assertIsNone(theta._last_request_id)
