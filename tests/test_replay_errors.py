from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import logging

import discord
from discord.ext import commands
from discord.ext.commands.view import StringView
import pytest
import config
from spy_setup.cog import SpySetupCog
from core.safe_logging import failure_location


@pytest.mark.asyncio
async def test_real_invoke_rejections_cooldown_and_concurrency(tmp_path, caplog):
    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    await bot._async_setup_hook()
    cog = SpySetupCog(bot, directory=tmp_path)
    cog._now = lambda: datetime(2026, 9, 14, 20, tzinfo=timezone.utc)
    cmd = cog.replay
    cmd.cog = cog

    def context(text='', channel=123):
        message = MagicMock()
        message.attachments = []
        message.channel = SimpleNamespace(id=channel)
        message.author = SimpleNamespace(id=1)
        ctx = commands.Context(message=message, bot=bot, view=StringView(text), prefix='!', command=cmd)
        ctx.send = AsyncMock()
        return ctx

    async def invoke(ctx):
        try:
            await cmd.invoke(ctx)
        except commands.CommandError as error:
            await cmd.dispatch_error(ctx, error)
        return ' '.join(str(c.args) for c in ctx.send.call_args_list)

    with patch.object(config, 'SPX_CHANNEL_ID', 123), patch.object(config, 'THETADATA_API_KEY', 'test'), patch('spy_setup.replay.replay_session', return_value=([], '0 triggered entries')) as provider:
        caplog.set_level(logging.INFO)
        assert 'today/future' in await invoke(context('2026-09-14'))
        assert not cog._replay_next
        assert 'private channel' in await invoke(context(channel=999))
        assert not cog._replay_next
        with patch.object(config, 'THETADATA_API_KEY', ''):
            assert 'credentials' in await invoke(context())
        assert not cog._replay_next
        cog._unloading = True
        assert 'shutdown' in await invoke(context())
        cog._unloading = False
        assert not cog._replay_next
        assert '0 triggered entries' in await invoke(context('2026-09-11'))
        reservation = dict(cog._replay_next)
        assert 'cooldown' in await invoke(context('2026-09-11'))
        assert 'private channel' in await invoke(context(channel=999))
        assert cog._replay_next == reservation
        assert provider.call_count == 1
        busy = context()
        await cmd._max_concurrency.acquire(busy)
        try:
            assert 'already running' in await invoke(context())
        finally:
            await cmd._max_concurrency.release(busy)
        cog._replay_next.clear()
        provider.side_effect = RuntimeError('SECRET_PROVIDER_PAYLOAD')
        assert 'No replay alerts generated' in await invoke(context())
        assert 'SECRET_PROVIDER_PAYLOAD' not in caplog.text
        assert 'RuntimeError' in caplog.text
        assert 'Replay delivered' in caplog.text
        assert 'cooldown remaining=' in caplog.text
    await bot.close()


def test_safe_diagnostics_excludes_messages_and_source():
    try:
        try:
            raise ValueError('SECRET_PROVIDER_TOKEN_AND_PAYLOAD')
        except ValueError as e:
            raise RuntimeError('SECRET_OUTER') from e
    except RuntimeError as error:
        text = failure_location(error)
    assert 'RuntimeError' in text and 'ValueError' in text
    assert 'test_replay_errors.py:' in text
    assert 'SECRET' not in text
