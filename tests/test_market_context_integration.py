"""Offline collector/status wiring; no live services."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.session_bars import session_time
from spy_setup.cog import SpySetupCog
from test_informational_integration import DAY, service  # noqa: F401


@pytest.mark.asyncio
async def test_alert_is_compact_and_read_only_status_keeps_cached_context(service):
    s = service
    await s.cog.poll_once()
    alert = s.channel.sent[0]['embed']
    assert not alert.fields and not alert.footer.text
    assert '\n' not in alert.description
    before_calls = len(s.provider.calls)
    before = {p: p.read_bytes() for p in s.directory.rglob('*.json')}
    ctx = SimpleNamespace(channel=SimpleNamespace(id=123), send=s.channel.send)
    await SpySetupCog.cmd_status.callback(s.cog, ctx)
    reply = s.channel.sent[-1]
    assert '5m + 10m' in reply['content']
    assert 'BB: 1m' not in reply['content']
    assert any('BB readiness: 5m warming up (3/21 candles)' in f.value
               for f in reply['embed'].fields)
    assert len(s.provider.calls) == before_calls
    assert before == {p: p.read_bytes() for p in s.directory.rglob('*.json')}


@pytest.mark.asyncio
async def test_backoff_stall_once_then_real_poll_recovery(service):
    s = service
    await s.cog.poll_once()
    s.channel.sent.clear()
    s.clock[0] = session_time(DAY, 9, 47) + timedelta(seconds=5)
    s.cog._pause_until = s.clock[0] + timedelta(minutes=5)
    calls = len(s.provider.calls)
    await s.cog.poll_once()
    await s.cog.poll_once()
    assert len(s.provider.calls) == calls
    assert [m['embed'].title for m in s.channel.sent] == ['⚠️ SPY FEED STALE']
    assert 'Last candle:' in s.channel.sent[0]['embed'].description

    original = s.provider.stock_history_ohlc

    def recovered(day, **kwargs):
        rows = original(day, **kwargs)
        if day == DAY:
            for minute in (15, 16):
                rows.append(dict(timestamp=(session_time(DAY, 9, 30) + timedelta(minutes=minute)).isoformat(),
                                 open=100., high=101., low=99., close=100., volume=100.))
        return rows

    s.provider.stock_history_ohlc = recovered
    s.cog._pause_until = None
    await s.cog.poll_once()
    await s.cog.poll_once()
    assert [m['embed'].title for m in s.channel.sent] == ['⚠️ SPY FEED STALE', '✅ SPY FEED FRESH AGAIN']
    assert 'Historical gaps may remain' in s.channel.sent[-1]['embed'].description
    assert all(not m['embed'].fields and not m['embed'].footer.text
               and '\n' not in m['embed'].description for m in s.channel.sent)
    journals = sorted((s.directory / 'feed-health-v1' / DAY.isoformat()).glob('*.json'))
    assert len(journals) == 2
