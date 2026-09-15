import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView
import pytest
import config
from core.session_bars import session_time
from core.state_store import StateStore, new_state
from spy_setup.cog import SpySetupCog
from spy_setup.daily import daily_embed, load_report_state, report_date

DAY = date(2026, 9, 14)


def test_report_only_observations_deduplicates_and_never_infers_outcomes():
    state = new_state(DAY)
    def item(lifecycle, **extra):
        return {'evaluated_at': session_time(DAY, 10).isoformat(), 'snapshot': {'classification': dict(setup_id='one', lifecycle=lifecycle, direction='bullish', **extra)}}
    state['observations'] = [item('forming'), item('forming'), item('triggered', entry_reference=100), item('triggered', entry_reference=100)]
    state['snapshot'] = {'events': [{'setup_id': 'invented', 'lifecycle': 'target_reached'}]}
    text = str(daily_embed(state, session_time(DAY, 16)).to_dict())
    assert 'Formed: 1 | Triggered entries: 1' in text
    assert 'Unknown: 1' in text and 'SPY target reached: 0' in text
    assert 'Missing: 390' in text
    assert 'NOT replay' in text
    state['observations'].append(item('target_reached', entry_reference=100))
    text = str(daily_embed(state, session_time(DAY, 16)).to_dict())
    assert 'SPY target reached: 1' in text and 'Unknown: 0' in text
    state['observations'] = [item('forming'), item('invalidated', entry_reference=100)]
    text = str(daily_embed(state, session_time(DAY, 16)).to_dict())
    assert 'Triggered entries: 0' in text
    assert 'Invalidated before observed entry: 0' in text
    assert 'Other unresolved / entry not observed: 1' in text


def test_read_missing_invalid_and_old_version_no_writes(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_report_state(store, DAY)
    state = new_state(DAY)
    store.save(state)
    before = store.path(DAY).read_bytes()
    load_report_state(store, DAY)
    assert store.path(DAY).read_bytes() == before
    store.path(DAY).write_text(before.decode().replace(state['version'], 'old-version'))
    with pytest.raises(ValueError):
        load_report_state(store, DAY)
    with pytest.raises(ValueError):
        report_date('2026-09-15', session_time(DAY, 16))


@pytest.mark.asyncio
async def test_actual_day_and_dynamic_cmd_dispatch(tmp_path):
    bot = commands.Bot(command_prefix='?', intents=discord.Intents.none())
    await bot._async_setup_hook()
    cog = SpySetupCog(bot, directory=tmp_path)
    cog._now = lambda: session_time(DAY, 16)
    with patch.object(cog, 'cog_load', AsyncMock()), patch.object(cog, 'cog_unload', AsyncMock()):
        await bot.add_cog(cog)
        async def invoke(name, text='', channel=123):
            message = MagicMock()
            message.attachments = []
            message.channel = SimpleNamespace(id=channel)
            ctx = commands.Context(message=message, bot=bot, view=StringView(text), prefix='?', command=bot.get_command(name))
            ctx.send = AsyncMock()
            await ctx.command.invoke(ctx)
            return ctx
        with patch.object(config, 'SPX_CHANNEL_ID', 123):
            ctx = await invoke('cmd')
            text = str(ctx.send.call_args_list)
            for name in ('day', 'cmd', 'replay', 'preview', 'status', 'help'):
                assert '?' + name in text
            assert 'private channel' in str((await invoke('day', channel=999)).send.call_args_list)
            assert 'unavailable' in str((await invoke('day')).send.call_args_list)
            cog.store.save(new_state(DAY))
            before = cog.store.path(DAY).read_bytes()
            assert (await invoke('day')).send.call_args.kwargs['embed'].title.endswith(str(DAY))
            assert cog.store.path(DAY).read_bytes() == before
        await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('day,hour', [(DAY, 16), (date(2026, 11, 27), 13)])
async def test_automatic_calendar_window_restart_dedup_and_flags(tmp_path, day, hour):
    channel = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_channel=lambda _: channel)
    cog = SpySetupCog(bot, theta_client=object(), directory=tmp_path)
    cog.store.save(new_state(day))
    clock = session_time(day, hour)
    cog._now = lambda: clock
    with patch.object(config, 'COLLECTION_ENABLED', True), patch.object(config, 'PUBLICATION_ENABLED', True), patch.object(config, 'THETADATA_API_KEY', 'test'), patch.object(config, 'SPX_CHANNEL_ID', 123):
        await cog.daily_once()
        channel.send.assert_not_awaited()
        clock += timedelta(minutes=6)
        with patch.object(config, 'PUBLICATION_ENABLED', False):
            await cog.daily_once()
        assert not list(tmp_path.glob('daily-*'))
        await cog.daily_once()
        await cog.daily_once()
        restart = SpySetupCog(bot, theta_client=object(), directory=tmp_path)
        restart._now = lambda: clock
        await restart.daily_once()
        channel.send.assert_awaited_once()
        assert 'sent' in next(tmp_path.glob('daily-*.json')).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['send', 'cancel', 'disk'])
async def test_unknown_send_and_disk_fail_closed(tmp_path, failure):
    channel = SimpleNamespace(send=AsyncMock())
    cog = SpySetupCog(SimpleNamespace(get_channel=lambda _: channel), theta_client=object(), directory=tmp_path)
    cog.store.save(new_state(DAY))
    cog._now = lambda: session_time(DAY, 16, 6)
    with patch.object(config, 'COLLECTION_ENABLED', True), patch.object(config, 'PUBLICATION_ENABLED', True), patch.object(config, 'THETADATA_API_KEY', 'test'), patch.object(config, 'SPX_CHANNEL_ID', 123):
        if failure == 'disk':
            with patch('spy_setup.daily.reserve_summary', side_effect=OSError):
                await cog.daily_once()
            channel.send.assert_not_awaited()
            return
        channel.send.side_effect = asyncio.CancelledError() if failure == 'cancel' else RuntimeError('send failure')
        if failure == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await cog.daily_once()
        else:
            await cog.daily_once()
        await cog.daily_once()
        channel.send.assert_awaited_once()
        assert 'unknown' in next(tmp_path.glob('daily-*.json')).read_text()


@pytest.mark.asyncio
async def test_monitor_postclose_no_provider_and_no_catchup(tmp_path):
    channel = SimpleNamespace(send=AsyncMock())
    provider = MagicMock()
    cog = SpySetupCog(SimpleNamespace(get_channel=lambda _: channel), theta_client=provider, directory=tmp_path)
    clock = session_time(DAY, 16, 6)
    cog._now = lambda: clock
    with patch.object(config, 'COLLECTION_ENABLED', True), patch.object(config, 'PUBLICATION_ENABLED', True), patch.object(config, 'THETADATA_API_KEY', 'test'), patch.object(config, 'SPX_CHANNEL_ID', 123):
        # No saved day is not a zero-signal report, and creates no journal.
        await cog.monitor.coro(cog)
        assert not list(tmp_path.glob('daily-*'))
        cog.store.save(new_state(DAY))
        clock = session_time(DAY, 16, 30)
        await cog.monitor.coro(cog)
        channel.send.assert_not_awaited()
        clock = session_time(DAY, 16, 6)
        with patch('spy_setup.daily.finish_summary', side_effect=OSError):
            await cog.monitor.coro(cog)
        await cog.monitor.coro(cog)
        channel.send.assert_awaited_once()
        provider.stock_history_ohlc.assert_not_called()
        assert 'unknown' in next(tmp_path.glob('daily-*.json')).read_text()
