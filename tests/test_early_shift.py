"""Offline synthetic rules and independent durable warning delivery."""
import asyncio
import copy
import json
from datetime import date

import pytest

from spy_setup.early_shift import build_events, VERSION
from spy_setup import early_shift_delivery as delivery
from spy_setup.signals import preceding_sessions
from test_informational import DAY, at, record
from test_informational_delivery import prepared, Channel


def history(length=390, samples=5, received=None):
    result = []
    for day in preceding_sessions(DAY)[:samples]:
        bars = record([100] * length, day=day)['bars']
        for bar in bars:
            bar['available_at'] = (received or at(0)).isoformat()
        result.extend(bars)
    return result


def candidate(closes=None):
    data = record(closes or [100] * 10 + [101])
    for bar in data['bars']:
        bar['volume'] = 150
    return data


@pytest.mark.parametrize('close,direction', [(101, 'above'), (99, 'below')])
def test_direction_volume_identity_and_immutable_inputs(close, direction):
    data, prior = candidate([100] * 10 + [close]), history()
    before = copy.deepcopy((data, prior))
    events = build_events(data, prior, at(11))
    assert len(events) == 1
    event = events[0]
    assert event['direction'] == direction
    assert event['relative_volume'] == 1.5 and event['volume_samples'] == 5
    assert event['version'] == VERSION and event['event_at'] == at(11, 0).isoformat()
    assert build_events(data, prior, at(11, 30)) == events
    assert (data, prior) == before
    embed = delivery.early_shift_embed(event)
    assert 'VOL 1.5×' in embed.title and 'EARLY' in embed.title
    assert '10m' in embed.description and 'VWAP' in embed.description and 'PDT' in embed.description
    assert not embed.fields and len(embed.description.splitlines()) == 1


@pytest.mark.parametrize('samples', [0, 1, 2])
def test_insufficient_history(samples):
    assert not build_events(candidate(), history(samples=samples), at(11))


def test_volume_threshold_touch_and_history_receipt_gate():
    data = candidate()
    data['bars'][-1]['volume'] = 149
    assert not build_events(data, history(), at(11))
    assert not build_events(candidate([100] * 10 + [100.1]), history(), at(11))
    assert not build_events(candidate(), history(received=at(11, 6)), at(11, 10))
    assert build_events(candidate(), history(samples=3), at(11))


@pytest.mark.parametrize('failure', ['gap', 'late', 'stale', 'future', 'invalid'])
def test_fail_closed(failure):
    data, now = candidate(), at(11)
    if failure == 'gap':
        data['bars'].pop(3)
    elif failure == 'late':
        data['bars'][-1]['available_at'] = at(11, 91).isoformat()
        now = at(11, 91)
    elif failure == 'stale':
        now = at(11, 91)
    elif failure == 'future':
        now = at(11, 0)
    else:
        data['bars'][-1]['volume'] = float('nan')
    assert not build_events(data, history(), now)


def test_cooldown_and_episode_reset_are_both_required():
    # Persistent outside expansion never repeats even after cooldown.
    data = candidate([100] * 10 + [101 + i for i in range(20)])
    assert not build_events(data, history(), at(30))
    # Return inside original 100.1 boundary, then break again before 15m: blocked.
    data = candidate([100] * 10 + [101] + [100] * 10 + [102])
    assert not build_events(data, history(), at(22))
    # Same reset followed by full cooldown permits a new event.
    data = candidate([100] * 10 + [101] + [100] * 14 + [102])
    assert len(build_events(data, history(), at(26))) == 1
    # Opposite direction also respects the shared 15m cooldown.
    events = build_events(candidate([100] * 10 + [101] + [99]), history(), at(12))
    assert all(event['direction'] == 'above' for event in events)


def test_freshness_boundary_and_early_close():
    assert build_events(candidate(), history(), at(11, 90))
    assert not build_events(candidate(), history(), at(11, 91))
    day = date(2026, 11, 27)
    data = record([100] * 209 + [101], day=day)
    # Exchange-calendar early close blocks even a newly completed candidate.
    assert not build_events(data, [], at(210, day=day))


def test_after_noon_and_market_close():
    data = candidate([100] * 160 + [101])
    assert build_events(data, history(), at(161))
    assert not build_events(candidate([100] * 389 + [101]), history(), at(390))


def test_late_backfill_cannot_reset_episode():
    data = candidate([100] * 10 + [101] + [100] * 13 + [101, 102])
    for index in range(11, 24):
        data['bars'][index]['available_at'] = at(26).isoformat()
    assert not build_events(data, history(), at(26))


def ready(tmp_path, monkeypatch, channel=None):
    cog, state, channel = prepared(tmp_path, monkeypatch, channel)
    state.update(record=candidate(), previous_bars=history())
    cog.now = at(11)
    return cog, state, channel


def intents(tmp_path):
    return list((tmp_path / 'early-shift-v1' / str(DAY)).glob('*.json'))


def test_delivery_separate_durable_and_restart_dedup(tmp_path, monkeypatch):
    async def run():
        cog, state, channel = ready(tmp_path, monkeypatch)
        before = copy.deepcopy(state)
        await delivery.publish_events(cog, state)
        restarted, new_state, _ = ready(tmp_path, monkeypatch, channel)
        await delivery.publish_events(restarted, new_state)
        assert len(channel.sent) == 1 and state == before
        assert {p.name for p in tmp_path.iterdir()} == {'early-shift-v1'}
        assert delivery.delivery_counts(tmp_path, DAY)['sent'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('flag', ['collecting', 'publishing'])
def test_disabled_is_suppressed_without_replay(tmp_path, monkeypatch, flag):
    async def run():
        cog, state, channel = ready(tmp_path, monkeypatch)
        setattr(cog, flag, False)
        await delivery.publish_events(cog, state)
        setattr(cog, flag, True)
        await delivery.publish_events(cog, state)
        assert not channel.sent
        assert delivery.delivery_counts(tmp_path, DAY)['suppressed'] == 1
    asyncio.run(run())


def test_ambiguous_send_never_retries(tmp_path, monkeypatch):
    async def run():
        channel = Channel(error=RuntimeError('private-secret'))
        cog, state, _ = ready(tmp_path, monkeypatch, channel)
        await delivery.publish_events(cog, state)
        channel.error = None
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
        assert delivery.delivery_counts(tmp_path, DAY)['unknown'] == 1
    asyncio.run(run())


def test_cancellation_preserves_intent(tmp_path, monkeypatch):
    async def run():
        channel = Channel(blocked=True)
        cog, state, _ = ready(tmp_path, monkeypatch, channel)
        task = asyncio.create_task(delivery.publish_events(cog, state))
        await channel.started.wait()
        assert json.loads(intents(tmp_path)[0].read_text())['status'] == 'unknown'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        channel.blocked = False
        await delivery.publish_events(cog, state)
        assert len(channel.sent) == 1
    asyncio.run(run())
