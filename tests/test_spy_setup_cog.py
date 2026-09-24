"""SPY collection, lifecycle delivery and durable failure boundaries."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
import config
from core.session_bars import utc
from core.state_store import validate_state
from spy_setup.cog import SpySetupCog, EvidenceError
from test_spy_setup_signals import DAY, record, history, breakout, retest, trigger


class Channel:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        if self.error:
            raise self.error


def prepared(tmp_path, monkeypatch, channel=None):
    monkeypatch.setattr(config, 'COLLECTION_ENABLED', True)
    monkeypatch.setattr(config, 'PUBLICATION_ENABLED', True)
    monkeypatch.setattr(config, 'THETADATA_API_KEY', 'offline-test')
    monkeypatch.setattr(config, 'SPX_CHANNEL_ID', 123)
    channel = channel or Channel()
    cog = SpySetupCog(SimpleNamespace(get_channel=lambda _: channel), object(), directory=tmp_path)
    state = cog._load(DAY)
    state['record'] = record([breakout()])
    state['previous_bars'] = sorted(history(), key=lambda row: row['timestamp'])
    for meta in state['prior_sessions'].values():
        meta['complete'] = True
    state['prior_complete'] = True
    cog._now = lambda: utc(state['record']['bars'][-1]['timestamp']) + timedelta(minutes=1, seconds=5)
    return cog, state, channel


def test_lifecycle_sent_once_restart_and_retest(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        await cog._assess(state)
        await cog._assess(state)
        assert len(channel.sent) == 1
        state = cog._load(DAY)
        await cog._assess(state)
        assert len(channel.sent) == 1
        state['record']['bars'].append(retest())
        cog._now = lambda: utc(retest()['available_at']) + timedelta(seconds=5)
        await cog._assess(state)
        assert len(channel.sent) == 2
        state['record']['bars'].append(trigger())
        cog._now = lambda: utc(trigger()['available_at']) + timedelta(seconds=5)
        await cog._assess(state)
        assert len(channel.sent) == 3
        assert state['last_observed_state']['lifecycle'] == 'triggered'
        validate_state(state, DAY)
    asyncio.run(run())


def test_ambiguous_send_never_retries(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch, Channel(RuntimeError('network')))
        await cog._assess(state)
        assert state['deliveries'][0]['delivery'] == 'unknown'
        state = cog._load(DAY)
        await cog._assess(state)
        assert len(channel.sent) == 1
    asyncio.run(run())


def test_publication_disabled_not_replayed(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        monkeypatch.setattr(config, 'PUBLICATION_ENABLED', False)
        await cog._assess(state)
        monkeypatch.setattr(config, 'PUBLICATION_ENABLED', True)
        await cog._assess(state)
        assert not channel.sent
        assert state['deliveries'][0]['delivery'] == 'suppressed'
    asyncio.run(run())


def test_disk_failure_prevents_send(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        def fail(_state):
            raise OSError('disk full')
        monkeypatch.setattr(cog.store, 'save', fail)
        with pytest.raises(EvidenceError):
            await cog._assess(state)
        assert not channel.sent
        assert cog._state is None
    asyncio.run(run())


def test_interrupted_pending_suppressed_or_unknown(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        monkeypatch.setattr(config, 'PUBLICATION_ENABLED', False)
        await cog._assess(state)
        entry = state['deliveries'][0]
        entry.update(delivery='pending', attempts=0, attempted_at=None)
        cog._save(state)
        assert cog.store.load(DAY)['deliveries'][0]['delivery'] == 'suppressed'
        entry.update(attempts=1, attempted_at=cog._now().isoformat())
        cog._save(state)
        assert cog.store.load(DAY)['deliveries'][0]['delivery'] == 'unknown'
    asyncio.run(run())


def test_event_freshness_not_extended_by_current_minute(tmp_path, monkeypatch):
    from test_spy_setup_signals import bar
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        await cog._assess(state)
        state['record']['bars'].extend([bar(DAY, 16, 101.3, 101.7, 101.25, 101.5),
                                      bar(DAY, 17, 101.5, 101.8, 101.3, 101.6)])
        cog._now = lambda: utc(state['record']['bars'][-1]['available_at']) + timedelta(seconds=5)
        await cog._assess(state)
        assert not cog._decision_fresh(state['snapshot'], cog._now())
        assert len(channel.sent) == 1
    asyncio.run(run())


def test_cancelled_provider_worker_retains_ownership(tmp_path, monkeypatch):
    import threading
    async def run():
        cog, _, _ = prepared(tmp_path, monkeypatch)
        started, release = threading.Event(), threading.Event()
        def worker():
            started.set()
            release.wait(3)
        task = asyncio.create_task(cog._call(worker))
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_collector_requests_spy_stock_ohlcv(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        calls = []
        class Provider:
            def stock_history_ohlc(self, day, **kwargs):
                calls.append((day, kwargs))
                return [breakout()]
        cog.theta_client = Provider()
        await cog.poll_once()
        assert len(calls) == 1
        assert calls[0][1]['symbol'] == 'SPY'
        assert calls[0][1]['interval'] == '1m'
        assert state['requests']['current']['succeeded'] == 1
        # One setup transition, two range notifications, one independent early warning.
        assert len(state['deliveries']) == 1
        assert state['deliveries'][0]['delivery'] == 'sent'
        assert len(channel.sent) == 4
        from spy_setup.early_shift_delivery import delivery_counts as early_counts
        assert early_counts(tmp_path, DAY)['sent'] == 1
        from spy_setup.informational_delivery import delivery_counts
        assert delivery_counts(tmp_path, DAY)['sent'] == 2
    asyncio.run(run())


def test_terminal_transition_persists(tmp_path, monkeypatch):
    from test_spy_setup_signals import bar
    async def run():
        cog, state, channel = prepared(tmp_path, monkeypatch)
        await cog._assess(state)
        for row in [retest(), trigger(), bar(DAY, 18, 101.6, 104, 100.5, 103)]:
            state['record']['bars'].append(row)
            cog._now = lambda row=row: utc(row['available_at']) + timedelta(seconds=5)
            await cog._assess(state)
        assert state['last_observed_state']['lifecycle'] == 'invalidated'
        assert len(channel.sent) == 4
        assert cog.store.load(DAY)['last_observed_state'] == state['last_observed_state']
    asyncio.run(run())
