"""New namespace integrity; old SPX evidence is never migrated."""
import asyncio
import copy
from types import SimpleNamespace
from datetime import timedelta

import pytest

from core.session_bars import new_record, update_record, validate_record, utc
from core.state_store import StateStore, new_state, validate_state
from test_spy_setup_signals import DAY, bar, record, history, breakout, retest, trigger
from spy_setup.cog import SpySetupCog
import config


def test_new_store_ignores_legacy_files(tmp_path):
    old = tmp_path / 'market-state' / f'state-{DAY}.json'
    old.parent.mkdir()
    old.write_text('broken legacy evidence')
    store = StateStore(tmp_path / 'spy-setups')
    state = store.load(DAY)
    assert len(state['prior_sessions']) == 5
    assert state['record']['bars'] == []
    store.save(state)
    assert store.load(DAY) == state
    assert old.read_text() == 'broken legacy evidence'


def test_volume_required_and_first_seen_preserved():
    rec = new_record(DAY)
    row = bar(DAY, 0)
    now = utc(row['available_at'])
    missing = dict(row)
    del missing['volume']
    update_record(rec, [missing], now)
    assert not rec['bars']
    update_record(rec, [row], now)
    update_record(rec, [dict(row, volume=999)], now + timedelta(minutes=1))
    assert rec['bars'][0]['volume'] == 100
    assert rec['bars'][0]['available_at'] == now.isoformat()
    validate_record(rec, DAY)


@pytest.mark.parametrize('volume', [None, -1, True, float('nan'), float('inf')])
def test_invalid_volume_rejected(volume):
    rec = new_record(DAY)
    row = bar(DAY, 0, volume=volume)
    update_record(rec, [row], utc(row['available_at']))
    assert not rec['bars']


def test_corrupt_state_fails_closed(tmp_path):
    store = StateStore(tmp_path)
    state = new_state(DAY)
    store.save(state)
    original = store.path(DAY).read_bytes()
    state['last_observed_state'] = {'direction': 'bullish'}
    with pytest.raises(ValueError):
        store.save(state)
    assert store.path(DAY).read_bytes() == original
    store.path(DAY).write_text('{"schema": 1, "schema": 1}')
    with pytest.raises(ValueError, match='Duplicate'):
        store.load(DAY)


def test_unavailable_snapshot_does_not_require_volatile_field(tmp_path):
    from spy_setup.signals import build_snapshot
    store = StateStore(tmp_path)
    state = new_state(DAY)
    state['snapshot'] = build_snapshot(state['record'], [], utc(bar(DAY, 0)['available_at']))
    state['snapshot']['classification'].pop('volatile', None)
    store.save(state)
    assert store.load(DAY) == state


def test_later_history_gap_fill_preserves_observation_and_lifecycle(tmp_path, monkeypatch):
    """A later receipt extends history, not an earlier observation's inputs."""
    monkeypatch.setattr(config, 'PUBLICATION_ENABLED', False)

    async def run():
        cog = SpySetupCog(SimpleNamespace(), directory=tmp_path)
        state = cog._load(DAY)
        state['record'] = record([breakout()])
        rows = sorted(history(), key=lambda row: row['timestamp'])
        missing = rows.pop(100)  # Unrelated to breakout/retest/trigger volume slots.
        state['previous_bars'] = rows
        missing_day = utc(missing['timestamp']).date().isoformat()
        for day, meta in state['prior_sessions'].items():
            meta['complete'] = day != missing_day
        state['prior_complete'] = False
        clock = utc(breakout()['available_at']) + timedelta(seconds=5)
        cog._now = lambda: clock
        await cog._assess(state)
        original = copy.deepcopy(state['observations'][0])
        assert original['snapshot']['classification']['phase'] == 'breakout'

        clock += timedelta(seconds=10)
        missing['available_at'] = clock.isoformat()
        state['previous_bars'] = sorted(rows + [missing], key=lambda row: row['timestamp'])
        state['prior_sessions'][missing_day]['complete'] = True
        state['prior_complete'] = True
        cog._save(state)
        state = cog._load(DAY)
        assert state['observations'][0] == original
        assert next(b for b in state['previous_bars']
                    if b['timestamp'] == missing['timestamp'])['available_at'] == clock.isoformat()

        for row, phase in ((retest(), 'retest'), (trigger(), 'active')):
            state['record']['bars'].append(row)
            clock = utc(row['available_at']) + timedelta(seconds=5)
            await cog._assess(state)
            state = cog._load(DAY)
            assert state['last_observed_state']['phase'] == phase
            assert state['observations'][0] == original
        assert state['last_observed_state']['lifecycle'] == 'triggered'
        assert len(state['observations']) == 3
        validate_state(state, DAY)

        # A named prior day must still have actually existed at assessment.
        corrupted = copy.deepcopy(state)
        old_as_of = utc(original['evaluated_at'])
        for row in corrupted['previous_bars']:
            if utc(row['timestamp']).date().isoformat() == missing_day:
                row['available_at'] = (old_as_of + timedelta(seconds=1)).isoformat()
        with pytest.raises(ValueError, match='Observation claims unavailable history'):
            validate_state(corrupted, DAY)

    asyncio.run(run())
