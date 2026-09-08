"""Neutral storage/migration tests: synthetic evidence only, no historical imports."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.session_bars import new_record, session_time, update_record, utc, validate_record
from core.state_store import StateStore, new_state, observation_id, validate_state

DAY = date(2026, 9, 8)
PRIOR, OLDER = date(2026, 9, 4), date(2026, 9, 3)


def bars(day, count, *, price=100):
    result = []
    for i in range(count):
        stamp = session_time(day, 9, 30) + timedelta(minutes=i)
        result.append(dict(timestamp=stamp.isoformat(), available_at=(stamp + timedelta(minutes=1)).isoformat(),
                           open=price, high=price + .1, low=price - .1, close=price))
    return result


def legacy_record(day=DAY, count=20):
    return dict(schema=1, rule='spx-orb-15m-first-break-v1', paper_only=True,
                session_date=day.isoformat(), bars=bars(day, count), candidate=None,
                outcome=None, status='waiting', polls=0, fetch_failures=0, rejected_bars=0)


def legacy_bb():
    state = new_state(DAY)
    state.update(version='bb-events-v1', record=legacy_record(), observed_bar=None)
    state['bar_request_ids'] = {b['timestamp']: 'original-current' for b in state['record']['bars']}
    state['previous_bars'] = bars(OLDER, 390) + bars(PRIOR, 390)
    state['prior_bar_request_ids'] = {b['timestamp']: 'original-prior' for b in state['previous_bars']}
    state['prior_complete'] = True
    for meta in state['prior_sessions'].values():
        meta['complete'] = True
    stamp = session_time(DAY, 9, 45).isoformat()
    event = dict(id=f'bb-events-v1/5/{stamp}/upper_touch', timeframe='5', bar_at=stamp, kind='upper_touch')
    state['deliveries'] = [dict(delivery='sent', event_ids=[event['id']],
                                snapshot=dict(version='bb-events-v1', session_date=DAY.isoformat(), events=[event]))]
    return state


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path.read_bytes()


def classified_snapshot(day=DAY, *, minute=20, direction='bullish', volatile=False, seconds=2):
    end = session_time(day, 9, 30) + timedelta(minutes=minute)
    frame = dict(available=False, reason='test', bar_at=None)
    return dict(version='spx-state-v1', session_date=day.isoformat(), as_of=(end + timedelta(seconds=seconds)).isoformat(),
                fresh=True, reason='complete', bar_at=(end - timedelta(minutes=1)).isoformat(), prior_days=[],
                timeframes={tf: dict(frame) for tf in ('1', '5', '15')},
                classification=dict(available=True, reason='complete', bar_at=(end - timedelta(minutes=5)).isoformat(),
                                    direction=direction, volatile=volatile, reasons=['synthetic test']))


def state_with_transition():
    state = new_state(DAY)
    state['record']['bars'] = bars(DAY, 20)
    snapshot = classified_snapshot()
    key = dict(direction='bullish', volatile=False)
    ident = observation_id(DAY.isoformat(), snapshot['classification'])
    state.update(snapshot=snapshot, observed_bar=snapshot['classification']['bar_at'], last_observed_state=key)
    state['observations'] = [dict(id=ident, snapshot=deepcopy(snapshot), evaluated_at=snapshot['as_of'],
                                  previous_state=None, transition=True, consumed_request_ids=dict(spx=[], prior_spx=[]),
                                  outcome_status='not_evaluated')]
    state['deliveries'] = [dict(id=ident, observation_id=ident, delivery='pending', attempted_at=None,
                                attempts=0, delivered_at=None, reason='state_transition')]
    return state


def test_neutral_ingestion_has_no_strategy_fields_and_retains_first_seen():
    record = new_record(DAY)
    rows = bars(DAY, 3)
    received = session_time(DAY, 9, 34)
    update_record(record, rows, received, data_cutoff=session_time(DAY, 9, 32))
    assert len(record['bars']) == 2
    assert all(b['available_at'] == received.isoformat() for b in record['bars'])
    original = deepcopy(record['bars'])
    revised = bars(DAY, 3, price=999)
    update_record(record, revised, session_time(DAY, 9, 35))
    assert record['bars'][:2] == original
    assert record['bars'][-1]['close'] == 999
    assert not {'candidate', 'outcome', 'rule', 'paper_only'} & set(record)
    validate_record(record, DAY)


def test_neutral_history_uses_actual_later_receipt():
    record = new_record(PRIOR)
    received = session_time(DAY, 10)
    update_record(record, bars(PRIOR, 390), received, data_cutoff=session_time(PRIOR, 16))
    assert len(record['bars']) == 390
    assert {row['available_at'] for row in record['bars']} == {received.isoformat()}


@pytest.mark.parametrize('invalid_copy', [False, True])
def test_duplicate_response_timestamp_rejected_even_if_one_copy_invalid(invalid_copy):
    record = new_record(DAY)
    rows = bars(DAY, 2)
    duplicate = dict(rows[0])
    if invalid_copy:
        duplicate['high'] = 1
    update_record(record, rows + [duplicate], session_time(DAY, 9, 33))
    assert [b['timestamp'] for b in record['bars']] == [rows[1]['timestamp']]
    assert record['rejected_bars'] >= 1


def test_iterator_failure_does_not_partially_mutate_record():
    record = new_record(DAY)
    original = deepcopy(record)
    def broken():
        yield bars(DAY, 1)[0]
        raise RuntimeError('transport iterator interrupted')
    with pytest.raises(RuntimeError):
        update_record(record, broken(), session_time(DAY, 10))
    assert record == original


@pytest.mark.parametrize('mutation', [
    lambda r: r['bars'].append(deepcopy(r['bars'][0])),
    lambda r: r['bars'].reverse(),
    lambda r: r['bars'][0].update(available_at=r['bars'][0]['timestamp']),
    lambda r: r['bars'][0].update(close=float('nan')),
    lambda r: r['bars'][0].update(close=True),
    lambda r: r.update(candidate=None),
    lambda r: r.update(polls=-1),
])
def test_neutral_record_rejects_corruption(mutation):
    record = dict(new_record(DAY), bars=bars(DAY, 2))
    mutation(record)
    with pytest.raises((ValueError, TypeError)):
        validate_record(record, DAY)


def test_new_store_roundtrip_and_namespace(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    state = store.load(DAY)
    assert not store.path(DAY).exists()
    store.save(state)
    assert store.path(DAY).name == f'state-{DAY}.json'
    assert store.load(DAY) == state
    assert not (tmp_path / 'audit').exists()


@pytest.mark.parametrize('attempts,expected', [(0, 'suppressed'), (1, 'unknown')])
def test_pending_recovery_does_not_claim_delivery_or_retry(tmp_path, attempts, expected):
    store = StateStore(tmp_path / 'market-state')
    state = state_with_transition()
    entry = state['deliveries'][0]
    if attempts:
        entry.update(attempts=1, attempted_at=state['snapshot']['as_of'])
    store.save(state)
    restored = store.load(DAY)
    assert restored['deliveries'][0]['delivery'] == expected
    assert restored['last_observed_state'] == dict(direction='bullish', volatile=False)
    assert restored['last_acknowledged_state'] is None
    store.save(restored)
    assert store.load(DAY) == restored


@pytest.mark.parametrize('mutation', [
    lambda s: s.update(version='unknown'),
    lambda s: s.update(schema=True),
    lambda s: s.update(last_observed_state=None),
    lambda s: s.update(observed_bar=None),
    lambda s: s['observations'].append(deepcopy(s['observations'][0])),
    lambda s: s['deliveries'].append(deepcopy(s['deliveries'][0])),
    lambda s: s['deliveries'].clear(),
    lambda s: s['observations'][0].update(transition=False),
    lambda s: s['observations'][0].update(previous_state=dict(direction='bearish', volatile=True)),
    lambda s: s['deliveries'][0].update(delivery='sent'),
    lambda s: s['deliveries'][0].update(delivery='unknown'),
    lambda s: s['observations'][0]['snapshot']['classification'].update(volatile=1),
    lambda s: s['observations'][0]['snapshot']['classification'].update(direction='volatile'),
    lambda s: s['observations'][0].update(evaluated_at=session_time(DAY, 10).isoformat()),
    lambda s: s['observations'][0]['consumed_request_ids'].update(spx=['invented']),
    lambda s: s['bar_request_ids'].update({'made-up': 'id'}),
    lambda s: s['requests']['current'].update(started=0, succeeded=1),
    lambda s: s['prior_sessions'][PRIOR.isoformat()].update(complete=True),
    lambda s: s['record']['bars'].pop(),
    lambda s: s['record']['bars'].pop(0),
    lambda s: s['record']['bars'][0].update(available_at=session_time(DAY, 10).isoformat()),
    lambda s: s['observations'][0]['snapshot'].update(prior_days=[str(PRIOR)]),
])
def test_invalid_new_ledger_fails_closed_without_reset(tmp_path, mutation):
    store = StateStore(tmp_path / 'market-state')
    state = state_with_transition()
    mutation(state)
    original = save_json(store.path(DAY), state)
    with pytest.raises((ValueError, KeyError, TypeError)):
        store.load(DAY)
    assert store.path(DAY).read_bytes() == original


def test_atomic_replace_failure_preserves_old_state_and_cleans_temp(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    state = store.load(DAY)
    store.save(state)
    before = store.path(DAY).read_bytes()
    state['poll_status'] = 'changed'
    with patch('core.state_store.os.replace', side_effect=OSError('disk failure')):
        with pytest.raises(OSError):
            store.save(state)
    assert store.path(DAY).read_bytes() == before
    assert list(store.directory.glob('*.tmp')) == []


def test_bb_migration_preserves_bars_history_lineage_without_legacy_deliveries(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    source = tmp_path / 'bb-events' / f'bb-events-{DAY}.json'
    legacy = legacy_bb()
    legacy['record']['candidate'] = dict(status='prospective', direction='UP',
                                         available_at=session_time(DAY, 9, 46).isoformat(),
                                         signal_bar=session_time(DAY, 9, 45).isoformat(),
                                         entry_time=session_time(DAY, 9, 47).isoformat())
    legacy['record']['outcome'] = dict(status='complete', points=2)
    original = save_json(source, legacy)
    state = store.load(DAY)
    assert state['record']['bars'] == legacy['record']['bars']
    assert state['previous_bars'] == legacy['previous_bars']
    assert state['bar_request_ids'] == legacy['bar_request_ids']
    assert state['prior_bar_request_ids'] == legacy['prior_bar_request_ids']
    assert state['prior_complete']
    assert state['observations'] == state['deliveries'] == []
    assert not {'candidate', 'outcome', 'rule'} & set(state['record'])
    assert state['migration'][0]['sha256'] == hashlib.sha256(original).hexdigest()
    store.save(state)
    assert store.load(DAY) == state
    assert source.read_bytes() == original


@pytest.mark.parametrize('sources', ['context', 'mirror', 'both'])
def test_context_and_orb_migration_only_copy_neutral_evidence(tmp_path, sources):
    store = StateStore(tmp_path / 'market-state')
    record = legacy_record()
    originals = {}
    if sources in ('context', 'both'):
        path = tmp_path / 'context' / f'context-{DAY}.json'
        originals[path] = save_json(path, dict(schema=1, version='spx-context-v1', session_date=DAY.isoformat(),
                                              record=record, bar_request_ids={record['bars'][0]['timestamp']: 'old-id'}))
    if sources in ('mirror', 'both'):
        path = tmp_path / 'audit' / f'orb-{DAY}.json'
        originals[path] = save_json(path, record)
    state = store.load(DAY)
    assert state['record']['bars'] == record['bars']
    assert len(state['migration']) == len(originals)
    assert bool(state['bar_request_ids']) == (sources != 'mirror')
    store.save(state)
    for path, original in originals.items():
        assert path.read_bytes() == original


def test_conflicting_context_and_orb_require_reconciliation(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    first, second = legacy_record(), legacy_record()
    second['status'] = 'different'
    paths = [tmp_path / 'context' / f'context-{DAY}.json', tmp_path / 'audit' / f'orb-{DAY}.json']
    original = [save_json(paths[0], dict(schema=1, version='spx-context-v1', session_date=DAY.isoformat(),
                                        record=first, bar_request_ids={})), save_json(paths[1], second)]
    with pytest.raises(ValueError, match='Conflicting'):
        store.load(DAY)
    assert not store.path(DAY).exists()
    assert [p.read_bytes() for p in paths] == original


@pytest.mark.parametrize('mutation', [
    lambda s: s.update(version='wrong'),
    lambda s: s['record'].pop('bars'),
    lambda s: s['record'].pop('candidate'),
    lambda s: s.update(polls=-1),
    lambda s: s.update(observed_bar='unknown'),
    lambda s: s.update(snapshot={'version': 'wrong'}),
    lambda s: s['record']['bars'][0].update(available_at=s['record']['bars'][0]['timestamp']),
    lambda s: s['bar_request_ids'].update({'outside-record': 'id'}),
    lambda s: s['previous_bars'].append(bars(date(2026, 9, 2), 1)[0]),
    lambda s: s['prior_sessions'][PRIOR.isoformat()].update(complete=False),
    lambda s: s['prior_bar_request_ids'].update({'missing-minute': 'id'}),
    lambda s: s['deliveries'][0].update(event_ids=['wrong']),
    lambda s: s['deliveries'].append(deepcopy(s['deliveries'][0])),
    lambda s: s['record'].update(candidate={'status': 'unrecognized'}),
])
def test_corrupt_bb_source_blocks_migration_no_fallback(tmp_path, mutation):
    store = StateStore(tmp_path / 'market-state')
    legacy = legacy_bb()
    mutation(legacy)
    source = tmp_path / 'bb-events' / f'bb-events-{DAY}.json'
    original = save_json(source, legacy)
    save_json(tmp_path / 'audit' / f'orb-{DAY}.json', legacy_record())
    with pytest.raises((ValueError, KeyError, TypeError)):
        store.load(DAY)
    assert source.read_bytes() == original
    assert not store.path(DAY).exists()


@pytest.mark.parametrize('payload', ['{"schema":1,"schema":2}', '{"number":NaN}', '{"number":1e999}', '{truncated'])
def test_malformed_source_does_not_get_overwritten(tmp_path, payload):
    store = StateStore(tmp_path / 'market-state')
    path = tmp_path / 'bb-events' / f'bb-events-{DAY}.json'
    path.parent.mkdir()
    path.write_text(payload)
    with pytest.raises((ValueError, KeyError)):
        store.load(DAY)
    assert path.read_text() == payload
    assert not store.path(DAY).exists()


def test_valid_new_store_never_reimports_changed_legacy(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    state = store.load(DAY)
    store.save(state)
    old = tmp_path / 'bb-events' / f'bb-events-{DAY}.json'
    old.parent.mkdir()
    old.write_text('corrupt retired source')
    assert store.load(DAY) == state


def test_only_requested_day_is_migrated_and_old_experiment_never_reconciled(tmp_path):
    store = StateStore(tmp_path / 'market-state')
    old = tmp_path / 'audit' / f'orb-{PRIOR}.json'
    original = save_json(old, legacy_record(PRIOR))
    state = store.load(DAY)
    assert state['record']['bars'] == []
    assert state['migration'] == []
    store.save(state)
    assert old.read_bytes() == original
