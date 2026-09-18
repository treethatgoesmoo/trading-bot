import asyncio
from datetime import date, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.session_bars import new_record, session_time
from spy_setup import feed_health as health

DAY = date(2026, 9, 15)


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(health.config, 'SPX_CHANNEL_ID', 123)
    clock = [session_time(DAY, 9, 35)]
    channel = SimpleNamespace(send=AsyncMock())
    enabled = [True, True]
    record = new_record(DAY)
    cog = SimpleNamespace(_now=lambda: clock[0], _unloading=False, _persistence_fault=False,
                          _collection_enabled=lambda: enabled[0], _publication_enabled=lambda: enabled[1],
                          _state={'session_date': str(DAY), 'record': record}, directory=tmp_path,
                          bot=SimpleNamespace(get_channel=lambda _: channel))
    return SimpleNamespace(cog=cog, clock=clock, channel=channel, enabled=enabled, record=record)


def fresh(s):
    stamp = s.clock[0].replace(second=0, microsecond=0) - timedelta(minutes=1)
    s.record['bars'].append(dict(timestamp=stamp.isoformat(), available_at=s.clock[0].isoformat(),
                                 open=100, high=100, low=100, close=100, volume=10))


def journals(s):
    return [json.loads(p.read_text()) for p in sorted(s.cog.directory.glob('feed-health-v1/*/*.json'))]


@pytest.mark.asyncio
async def test_repeat_restart_recovery_and_second_outage(service):
    s = service
    await health.check_feed_health(s.cog)
    await health.check_feed_health(SimpleNamespace(**vars(s.cog)))  # No in-memory dedup state.
    assert s.channel.send.await_count == 1
    fresh(s)
    await health.check_feed_health(s.cog)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 2
    s.clock[0] += timedelta(seconds=91)
    await health.check_feed_health(s.cog)
    assert [j['kind'] for j in journals(s)] == ['stall', 'recovery', 'stall']
    assert s.channel.send.await_count == 3


@pytest.mark.asyncio
async def test_disabled_stall_never_replayed_and_no_recovery(service):
    s = service
    s.enabled[1] = False
    await health.check_feed_health(s.cog)
    s.enabled[1] = True
    await health.check_feed_health(s.cog)
    fresh(s)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 0
    assert [j['reason'] for j in journals(s)] == ['publication_disabled', 'stall_not_acknowledged']


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('secret payload'), TimeoutError(), asyncio.CancelledError()])
async def test_unknown_send_never_retried_or_followed_by_recovery(service, error, caplog):
    s = service
    s.channel.send.side_effect = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await health.check_feed_health(s.cog)
    else:
        await health.check_feed_health(s.cog)
    s.channel.send.side_effect = None
    await health.check_feed_health(s.cog)
    fresh(s)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1
    assert journals(s)[0]['status'] == 'unknown'
    assert 'secret payload' not in caplog.text


@pytest.mark.asyncio
async def test_startup_grace_and_exact_staleness_boundary(service):
    s = service
    s.clock[0] = session_time(DAY, 9, 32) + timedelta(seconds=30)
    await health.check_feed_health(s.cog)
    assert not journals(s)
    s.clock[0] += timedelta(microseconds=1)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['closed', 'disabled', 'shutdown', 'fault', 'missing', 'invalid'])
async def test_fail_closed(service, mode):
    s = service
    if mode == 'closed': s.clock[0] = session_time(DAY, 16)
    if mode == 'disabled': s.enabled[0] = False
    if mode == 'shutdown': s.cog._unloading = True
    if mode == 'fault': s.cog._persistence_fault = True
    if mode == 'missing': s.cog._state = None
    if mode == 'invalid': s.record['schema'] = 99
    await health.check_feed_health(s.cog)
    assert not journals(s)
    s.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_reservation_blocks_all_sends(service):
    s = service
    await health.check_feed_health(s.cog)
    path = next(s.cog.directory.glob('feed-health-v1/*/*.json'))
    path.write_text('{')
    fresh(s)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1
    assert path.read_text() == '{'


@pytest.mark.asyncio
async def test_market_closes_before_send(service, monkeypatch):
    s = service
    original = health._write
    def write(*args, **kwargs):
        original(*args, **kwargs)
        s.clock[0] = session_time(DAY, 16)
    monkeypatch.setattr(health, '_write', write)
    await health.check_feed_health(s.cog)
    s.channel.send.assert_not_awaited()
    assert journals(s)[0]['status'] == 'suppressed'


@pytest.mark.asyncio
async def test_close_during_await_cannot_cause_followup_send(service):
    s = service
    async def send(**kwargs):
        assert journals(s)[0]['status'] == 'unknown'
        s.clock[0] = session_time(DAY, 16)
    s.channel.send.side_effect = send
    await health.check_feed_health(s.cog)
    fresh(s)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1


@pytest.mark.asyncio
async def test_old_candle_with_new_fetch_time_is_still_stale(service):
    s = service
    fresh(s)
    s.clock[0] += timedelta(minutes=4)
    s.record['last_success_at'] = s.clock[0].isoformat()
    await health.check_feed_health(s.cog)
    assert journals(s)[0]['kind'] == 'stall'


@pytest.mark.asyncio
async def test_fresh_feed_independent_of_history_or_setup(service):
    s = service
    fresh(s)
    await health.check_feed_health(s.cog)
    assert not journals(s)
    s.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_early_close_is_exclusive(service):
    s = service
    early = date(2026, 11, 27)
    s.clock[0] = session_time(early, 13)
    s.cog._state = {'session_date': str(early), 'record': new_record(early)}
    await health.check_feed_health(s.cog)
    assert not journals(s)
    s.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_channel_is_not_retried(service):
    s = service
    s.cog.bot.get_channel = lambda _: None
    await health.check_feed_health(s.cog)
    s.cog.bot.get_channel = lambda _: s.channel
    await health.check_feed_health(s.cog)
    fresh(s)
    await health.check_feed_health(s.cog)
    s.channel.send.assert_not_awaited()
    assert journals(s)[0]['reason'] == 'channel_unavailable'


@pytest.mark.asyncio
async def test_reservation_failure_prevents_send(service, monkeypatch):
    s = service
    def fail(*args, **kwargs):
        raise OSError('private filesystem details')
    monkeypatch.setattr(health, '_write', fail)
    await health.check_feed_health(s.cog)
    s.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_receipts_do_not_recover_feed(service):
    s = service
    await health.check_feed_health(s.cog)
    fresh(s)
    s.record['bars'][-1]['available_at'] = (s.clock[0] + timedelta(minutes=1)).isoformat()
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1


@pytest.mark.asyncio
async def test_ack_write_failure_preserves_unknown_and_suppresses_recovery(service, monkeypatch):
    s = service
    original = health._write
    def write(path, item, **kwargs):
        if not kwargs.get('exclusive'): raise OSError('private detail')
        return original(path, item, **kwargs)
    monkeypatch.setattr(health, '_write', write)
    await health.check_feed_health(s.cog)
    assert journals(s)[0]['status'] == 'unknown'
    fresh(s)
    await health.check_feed_health(s.cog)
    assert s.channel.send.await_count == 1
