"""Command boundary checks; all provider work and Discord sends are mocked."""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

import config
from core.session_bars import session_time
from spy_setup.cog import SpySetupCog
from spy_setup.projected_range import RangeResult

DAY = date(2026, 9, 15)
NOW = session_time(DAY, 10)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'SPX_CHANNEL_ID', 123)
    monkeypatch.setattr(config, 'THETADATA_API_KEY', 'fictional')
    monkeypatch.setattr(config, 'COLLECTION_ENABLED', True)
    cog = SpySetupCog(None, object(), directory=tmp_path)
    cog._now = lambda: NOW
    result = RangeResult(discord.Embed(title='Synthetic range'), {'synthetic': True}, 100., NOW)
    cog._call = AsyncMock(return_value=result)
    ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
    return cog, ctx, result


async def invoke(cog, ctx):
    await cog.projected_range.callback(cog, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['private', 'disabled', 'credentials', 'shutdown', 'preopen', 'holiday'])
async def test_rejections_make_no_requests(setup, monkeypatch, reason):
    cog, ctx, _ = setup
    if reason == 'private':
        ctx.channel.id = 999
    elif reason == 'disabled':
        monkeypatch.setattr(config, 'COLLECTION_ENABLED', False)
    elif reason == 'credentials':
        monkeypatch.setattr(config, 'THETADATA_API_KEY', '')
    elif reason == 'shutdown':
        cog._unloading = True
    elif reason == 'preopen':
        cog._now = lambda: session_time(DAY, 9, 30)
    else:
        cog._now = lambda: session_time(date(2026, 9, 7), 10)
    await invoke(cog, ctx)
    cog._call.assert_not_called()
    assert cog._range_next == 0
    assert not cog._replay_tasks


@pytest.mark.asyncio
async def test_success_cache_cooldown_and_day_rollover(setup, tmp_path):
    cog, ctx, result = setup
    await invoke(cog, ctx)
    assert ctx.send.call_args.kwargs['embed'] is result.embed
    assert cog._range_cache == (DAY, result.history, result.anchor)
    assert not list(tmp_path.iterdir())  # no live journals/evidence written
    with pytest.raises(commands.CommandOnCooldown):
        await invoke(cog, ctx)
    cog._range_next = 0
    await invoke(cog, ctx)
    assert cog._call.call_args.kwargs == {'history': result.history, 'anchor': 100.}
    cog._range_next = 0
    tomorrow = DAY + timedelta(days=1)
    cog._now = lambda: session_time(tomorrow, 10)
    cog._call.return_value = RangeResult(result.embed, {}, 101., session_time(tomorrow, 10))
    await invoke(cog, ctx)
    assert cog._call.call_args.kwargs == {'history': None, 'anchor': None}
    assert not cog._replay_tasks


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_no_retry(setup):
    cog, ctx, _ = setup
    cog._call.side_effect = RuntimeError('SECRET provider payload')
    await invoke(cog, ctx)
    assert cog._call.await_count == 1
    assert 'unavailable' in ctx.send.call_args.args[0]
    assert 'SECRET' not in str(ctx.send.call_args_list)
    assert not cog._replay_tasks


@pytest.mark.asyncio
async def test_slow_history_refreshes_once_with_retained_anchor(setup):
    cog, ctx, result = setup
    later = NOW + timedelta(seconds=40)
    times = iter([NOW, later, later, later])
    cog._now = lambda: next(times)
    cog._call.side_effect = [result, RangeResult(result.embed, result.history, 100., NOW)]
    await invoke(cog, ctx)
    assert cog._call.await_count == 2
    assert cog._call.call_args.args[2] == later
    assert cog._call.call_args.kwargs == {'history': result.history, 'anchor': 100.}
    assert 'embed' in ctx.send.call_args.kwargs


@pytest.mark.asyncio
async def test_final_gate_checks_candle_age_not_only_processing_time(setup):
    cog, ctx, result = setup
    times = iter([NOW, NOW + timedelta(seconds=20), NOW + timedelta(seconds=20)])
    cog._now = lambda: next(times)
    cog._call.return_value = RangeResult(result.embed, result.history, 100., NOW - timedelta(seconds=90))
    await invoke(cog, ctx)
    assert 'unavailable' in ctx.send.call_args.args[0]
    assert not any('embed' in call.kwargs for call in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_cancelled_range_is_removed_from_shutdown_tracking(setup):
    cog, ctx, _ = setup
    cog._call.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await invoke(cog, ctx)
    assert not cog._replay_tasks


@pytest.mark.asyncio
async def test_command_concurrency_and_error_feedback(setup):
    cog, ctx, _ = setup
    gate = cog.projected_range._max_concurrency
    await gate.acquire(ctx)
    try:
        with pytest.raises(commands.MaxConcurrencyReached):
            await gate.acquire(ctx)
    finally:
        await gate.release(ctx)
    await cog.projected_range_error(ctx, commands.MaxConcurrencyReached(1, commands.BucketType.default))
    assert 'already running' in ctx.send.call_args.args[0]
