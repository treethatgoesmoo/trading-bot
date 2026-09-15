import asyncio
import copy
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import threading

import pytest
import config
from core import market_calendar
from core.session_bars import session_time
from spy_setup.cog import SpySetupCog
from spy_setup.replay import LABEL, replay_date, replay_session

DAY = date(2026, 9, 8)
NOW = session_time(date(2026, 9, 9), 10)


class Provider:
    def __init__(self, *, gap=False, fail=False, events=True):
        self.calls = []
        self.closed = False
        self.gap, self.fail, self.events = gap, fail, events
        self.responses = []

    def stock_history_ohlc(self, day, **kwargs):
        self.calls.append((day, kwargs))
        if self.fail:
            raise RuntimeError('PERMISSION_DENIED secret-token must not leak')
        count = int((session_time(day, *market_calendar.close_time(day)) - session_time(day, 9, 30)).total_seconds() / 60)
        rows = [dict(timestamp=session_time(day, 9, 30) + timedelta(minutes=i), open=100, high=101, low=99, close=100, volume=100) for i in range(count)]
        if day == DAY and self.events:
            for i, values in [(15, (100, 101.5, 100, 101.3, 200)), (16, (101.3, 101.4, 101, 101.2, 100)), (17, (101.2, 101.7, 101.1, 101.6, 200))]:
                rows[i].update(zip(('open', 'high', 'low', 'close', 'volume'), values))
        if self.gap:
            rows.pop(20)
        self.responses.append(rows)
        return rows

    def close(self):
        self.closed = True


def test_dates_default_previous_even_after_close_and_reject_bad_inputs():
    assert replay_date(None, NOW) == DAY
    assert replay_date(None, session_time(date(2026, 9, 8), 18)) == date(2026, 9, 4)  # holiday/weekend
    for value in ['2026-09-09', '2026-09-10', '2026-08-01', '2026-09-07', '2026-02-30', '26-9-8', '2026-09-08 extra']:
        with pytest.raises(ValueError):
            replay_date(value, NOW)


def test_actual_formatter_chronology_receipt_isolation_and_six_requests():
    provider = Provider()
    originals = []
    method = provider.stock_history_ohlc
    def fetch(*args, **kwargs):
        rows = method(*args, **kwargs)
        originals.append(copy.deepcopy(rows))
        return rows
    provider.stock_history_ohlc = fetch
    embeds, summary = replay_session(DAY, NOW, client_factory=lambda **_: provider)
    assert len(provider.calls) == 6 and provider.closed
    assert provider.responses == originals  # no fabricated receipts on source objects
    assert len(embeds) == 4
    assert ['FORMING' in e.title for e in embeds[:2]] == [True, True]
    assert 'TRIGGERED' in embeds[2].title and 'INVALIDATED' in embeds[3].title
    assert '1 triggered entries' in summary
    for embed in embeds:
        assert LABEL in embed.title and LABEL in embed.footer.text
        assert str(DAY) in embed.title and 'Assumes history' in embed.description
        assert 'PDT' in embed.fields[0].value and len(embed) < 6000
        assert len(embed.title) <= 256


def test_no_events_is_explicit():
    embeds, summary = replay_session(DAY, NOW, client_factory=lambda **_: Provider(events=False))
    assert embeds == [] and '0 triggered entries' in summary


@pytest.mark.parametrize('kwargs', [{'gap': True}, {'fail': True}])
def test_provider_error_or_gap_fails_closed_and_closes(kwargs):
    provider = Provider(**kwargs)
    with pytest.raises((ValueError, RuntimeError)):
        replay_session(DAY, NOW, client_factory=lambda **_: provider)
    assert provider.closed and len(provider.calls) == 1


@pytest.mark.asyncio
async def test_command_is_private_and_errors_sanitized(tmp_path):
    cog = SpySetupCog(None, theta_client=object(), directory=tmp_path)
    cog._now = lambda: NOW
    cog._state = {'sentinel': 'unchanged'}
    original = copy.deepcopy(cog._state)
    ctx = SimpleNamespace(channel=SimpleNamespace(id=456), send=AsyncMock())
    with patch.object(config, 'SPX_CHANNEL_ID', 123), patch.object(config, 'THETADATA_API_KEY', 'test'), patch('spy_setup.replay.replay_session', side_effect=RuntimeError('secret-token')) as run:
        await SpySetupCog.replay.callback(cog, ctx)
        run.assert_not_called()
        ctx.channel.id = 123
        await SpySetupCog.replay.callback(cog, ctx, '2026-09-09')
        run.assert_not_called()
        with patch.object(config, 'COLLECTION_ENABLED', False), patch.object(config, 'PUBLICATION_ENABLED', False):
            await SpySetupCog.replay.callback(cog, ctx)
        run.assert_called_once_with(DAY, NOW)
    assert 'secret-token' not in str(ctx.send.call_args_list)
    assert cog._state == original and not list(tmp_path.iterdir())
    assert not cog._replay_tasks


@pytest.mark.asyncio
async def test_cancel_retains_worker_until_client_closed_and_collector_independent(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    cog = SpySetupCog(None, theta_client=object(), directory=tmp_path)
    cog._now = lambda: NOW
    ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
    def worker(*args):
        entered.set()
        try:
            release.wait(5)
            return [], 'done'
        finally:
            closed.set()
    with patch.object(config, 'SPX_CHANNEL_ID', 123), patch.object(config, 'THETADATA_API_KEY', 'test'), patch('spy_setup.replay.replay_session', side_effect=worker):
        task = asyncio.create_task(SpySetupCog.replay.callback(cog, ctx))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not cog._lock.locked()  # live collector not blocked by replay
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and task in cog._replay_tasks
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set() and not cog._replay_tasks
    assert ctx.send.await_count == 1


def test_command_bounds():
    cmd = SpySetupCog.replay
    assert not cmd._buckets.valid  # cooldown reserved only after callback validation
    assert cmd._max_concurrency.number == 1 and not cmd._max_concurrency.wait


@pytest.mark.asyncio
async def test_success_posts_ordered_embeds_then_summary_without_delivery(tmp_path):
    cog = SpySetupCog(None, theta_client=object(), directory=tmp_path)
    cog._now = lambda: NOW
    cog._deliver = AsyncMock(side_effect=AssertionError('must not use live delivery'))
    ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=AsyncMock())
    embeds = [object(), object()]
    with patch.object(config, 'SPX_CHANNEL_ID', 123), patch.object(config, 'THETADATA_API_KEY', 'test'), patch('spy_setup.replay.replay_session', return_value=(embeds, 'summary')):
        await SpySetupCog.replay.callback(cog, ctx)
    assert ctx.send.call_args_list[1].kwargs == {'embed': embeds[0]}
    assert ctx.send.call_args_list[2].kwargs == {'embed': embeds[1]}
    assert ctx.send.call_args_list[3].args == ('summary',)
    cog._deliver.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_early_close_request_uses_calendar_and_pacific_standard_time():
    day = date(2026, 11, 27)
    now = session_time(date(2026, 11, 30), 10)
    provider = Provider(events=False)
    embeds, summary = replay_session(day, now, client_factory=lambda **_: provider)
    assert provider.calls[0][1]['end_time'] == '13:00:00'
    assert len(provider.responses[0]) == 210
    assert embeds == [] and '0 triggered entries' in summary
