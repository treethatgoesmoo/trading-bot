"""Single-stream assembly safety and preserved historical audit helpers."""
import os
import sys
import types
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from zero_dte.flow_cog import SPXFlowCog
from zero_dte.orb_audit import session_time


class ReplacementAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def assemble(self, *, mode='legacy', key='fake', channel=123, token='fake'):
        import bot.core as core
        bot = MagicMock()
        bot.__aenter__ = AsyncMock(return_value=bot)
        bot.__aexit__ = AsyncMock(return_value=False)
        bot.add_cog = AsyncMock()
        bot.start = AsyncMock()
        # Isolate assembly from polling/delivery, tested in the BB cog suite.
        class BBSignalCog:
            def __init__(self, bot, theta_client):
                self.bot, self.theta_client = bot, theta_client
        module = types.ModuleType('zero_dte.bb_signal_cog')
        module.BBSignalCog = BBSignalCog
        theta = MagicMock()
        retired = dict(NOTIFICATION_MODE=mode, ZERO_DTE_ENABLED=True,
                       SWING_ENABLED=True, SPX_FLOW_ENABLED=True,
                       SPX_CONTEXT_FLOW_ENABLED=True,
                       THETA_AUDIT_START_DATE='2026-09-08', THETA_AUDIT_FLOW_ENABLED=True)
        env = {name: str(value) for name, value in retired.items()}
        with patch.multiple(core, DISCORD_TOKEN=token, THETADATA_API_KEY=key,
                            SPX_CONTEXT_CHANNEL_ID=channel), \
                patch.multiple(core, create=True, **retired), \
                patch.dict(os.environ, env), \
                patch.object(core, 'build_bot', return_value=bot), \
                patch.dict(sys.modules, {'zero_dte.bb_signal_cog': module}), \
                patch('core.thetadata_client.ThetaDataClient', return_value=theta) as factory:
            await core.run_bot()
        return bot, [call.args[0] for call in bot.add_cog.call_args_list], factory

    async def test_replaces_every_old_stream_with_one_bb_collector(self):
        bot, cogs, factory = await self.assemble()
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'BBSignalCog'])
        factory.assert_called_once_with(purpose='bb_events')
        bot.start.assert_awaited_once_with('fake')

    async def test_missing_key_never_falls_back_to_old_notifications(self):
        _, cogs, factory = await self.assemble(key='')
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog'])
        factory.assert_not_called()

    async def test_obsolete_mode_values_cannot_select_a_different_stream(self):
        for mode in ('legacy', 'spx', 'typo', ''):
            with self.subTest(mode=mode):
                _, cogs, factory = await self.assemble(mode=mode)
                self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'BBSignalCog'])
                factory.assert_called_once_with(purpose='bb_events')

    async def test_missing_channel_keeps_only_local_bb_collection_no_fallback(self):
        with self.assertLogs('bot.core', level='WARNING') as logs:
            _, cogs, factory = await self.assemble(channel=0)
        self.assertEqual([type(c).__name__ for c in cogs], ['SharedCog', 'BBSignalCog'])
        factory.assert_called_once_with(purpose='bb_events')
        self.assertIn('collecting locally, no notifications', '\n'.join(logs.output))

    async def test_missing_discord_token_does_not_start_any_cog(self):
        bot, cogs, factory = await self.assemble(token='')
        self.assertEqual(cogs, [])
        factory.assert_not_called()
        bot.start.assert_not_awaited()

    async def test_shared_legacy_posts_tuning_and_commands_are_removed(self):
        from bot.shared_cog import SharedCog
        cog = SharedCog(MagicMock())
        self.assertEqual({command.name for command in cog.get_commands()}, {'cmd', 'commit'})
        for method in ('on_ready', 'maybe_post_econ_briefing', 'maybe_tune_weights',
                       'cmd_events', 'cmd_stats', 'cmd_dashboard'):
            self.assertFalse(hasattr(cog, method), method)
        self.assertTrue(cog.cmd_commit.checks, 'Git administration must require owner authorization')

    async def test_history_housekeeping_is_quiet_and_once_per_day(self):
        from bot.shared_cog import SharedCog
        bot = MagicMock()
        cog = SharedCog(bot)
        with patch('bot.shared_cog.GIT_AUTOCOMMIT_ENABLED', True), \
                patch('bot.shared_cog.commit_data_history', return_value=(True, 'saved')) as commit:
            await cog.maybe_commit_history('2026-09-08')
            await cog.maybe_commit_history('2026-09-08')
        commit.assert_called_once()
        bot.get_channel.assert_not_called()

    async def test_manual_history_command_cannot_use_an_old_or_unconfigured_channel(self):
        from bot.shared_cog import SharedCog
        cog = SharedCog(MagicMock())
        ctx = MagicMock()
        ctx.channel.id = 999
        ctx.send = AsyncMock()
        for channel_id in (0, 123):
            with self.subTest(channel_id=channel_id), \
                    patch('bot.shared_cog.SPX_CONTEXT_CHANNEL_ID', channel_id), \
                    patch('bot.shared_cog.commit_data_history') as commit:
                await cog.cmd_commit.callback(cog, ctx)
                commit.assert_not_called()


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
