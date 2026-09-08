"""Offline review contracts; synthetic state only, no runtime or transport."""
from copy import deepcopy
import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from core.session_bars import session_time
from core.state_store import new_state, observation_id, validate_state
from market_state import review

DAY = date(2026, 9, 8)
ROOT = Path(__file__).resolve().parents[1]


def fixture_state():
    state = new_state(DAY)
    opening = session_time(DAY, 9, 30)
    for i in range(55):
        stamp = opening + timedelta(minutes=i)
        state['record']['bars'].append(dict(timestamp=stamp.isoformat(),
            available_at=(stamp + timedelta(minutes=1)).isoformat(),
            open=12345.67, high=12346.0, low=12345.0, close=12345.67))
    sequence = [('neutral', False, None), ('bullish', False, 'sent'),
                ('bullish', False, None), ('bearish', True, 'failed'),
                ('bearish', True, None), ('neutral', True, 'unknown'),
                ('neutral', False, 'suppressed'), ('bullish', False, 'pending')]
    previous = None
    for index, (direction, volatile, delivery) in enumerate(sequence):
        end = opening + timedelta(minutes=20 + index * 5)
        when = end + timedelta(seconds=2)
        classification = dict(available=True, reason='complete', bar_at=(end - timedelta(minutes=5)).isoformat(),
                              direction=direction, volatile=volatile, reasons=['synthetic'])
        snapshot = dict(version='spx-state-v1', session_date=DAY.isoformat(), as_of=when.isoformat(),
                        fresh=True, reason='complete', bar_at=(end - timedelta(minutes=1)).isoformat(),
                        prior_days=[], timeframes={tf: dict(available=False) for tf in ('1', '5', '15')},
                        classification=classification)
        ident = observation_id(DAY.isoformat(), classification)
        state['observations'].append(dict(id=ident, snapshot=snapshot, evaluated_at=when.isoformat(),
            previous_state=deepcopy(previous), transition=delivery is not None,
            consumed_request_ids=dict(spx=[], prior_spx=[]), outcome_status='not_evaluated'))
        previous = dict(direction=direction, volatile=volatile)
        if delivery:
            attempted = delivery in ('sent', 'unknown', 'failed')
            state['deliveries'].append(dict(id=ident, observation_id=ident, delivery=delivery,
                attempts=int(attempted), attempted_at=when.isoformat() if attempted else None,
                delivered_at=(when + timedelta(seconds=1)).isoformat() if delivery == 'sent' else None,
                reason='synthetic'))
            if delivery == 'sent':
                state['last_acknowledged_state'] = deepcopy(previous)
        state['snapshot'] = deepcopy(snapshot)
        state['observed_bar'] = classification['bar_at']
    state['last_observed_state'] = previous
    state['polls'] = state['record']['polls'] = 10
    state['requests'] = dict(current=dict(started=10, succeeded=6, failed=2),
                             prior=dict(started=4, succeeded=2, failed=1))
    validate_state(state, DAY)
    return state


def write_fixture(directory, state=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'state-{DAY}.json'
    path.write_text(json.dumps(fixture_state() if state is None else state), encoding='utf-8')
    return path


def test_summary_counts_direction_and_volatility_independently():
    state = fixture_state()
    original = deepcopy(state)
    text = review.summary(state)
    assert 'Current requests: attempted=10, succeeded=6, failed=2, incomplete=2' in text
    assert 'Prior requests: attempted=4, succeeded=2, failed=1, incomplete=1' in text
    assert 'Recorded observations: 8; transitions: 5; non-transition observations: 3' in text
    assert 'Direction: bullish=3, bearish=2, neutral=3' in text
    assert 'volatile=3, not_volatile=5' in text
    for key in ('sent', 'failed', 'unknown', 'suppressed', 'pending'):
        assert f'{key}=1' in text
    assert 'Local completed minute bars: 55; prior-history bars: 0' in text
    assert 'Migration sources recorded: 0' in text
    assert 'not full-session coverage' in text
    assert 'not evaluated' in text and 'not billing units' in text
    assert 'owner usefulness are not measured' in text
    assert state == original
    assert state['deliveries'][-1]['delivery'] == 'pending'


def test_read_only_summary_does_not_migrate_reconcile_or_write(tmp_path, capsys):
    path = write_fixture(tmp_path)
    before = path.read_bytes()
    with patch('core.state_store.StateStore.load', side_effect=AssertionError('must not load store')), \
            patch('core.state_store.StateStore.save', side_effect=AssertionError('must not save store')):
        assert review.main([str(DAY), '--directory', str(tmp_path)]) == 0
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    assert 'pending=1' in capsys.readouterr().out


def test_csv_contains_each_observation_not_only_sent_and_no_price_fields(tmp_path, capsys):
    source = write_fixture(tmp_path)
    before = source.read_bytes()
    target = tmp_path / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 0
    with target.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 8
    assert set(rows[0]) == set(review.CSV_FIELDS)
    assert [row['delivery_status'] for row in rows] == [
        'initial_silent', 'sent', 'unchanged', 'failed', 'unchanged', 'unknown', 'suppressed', 'pending']
    assert rows[0]['candle_completed_et'] == '2026-09-08T09:50:00-04:00'
    assert rows[3]['direction'] == 'bearish' and rows[3]['volatile'] == 'true'
    assert all(row['usefulness'] == row['notes'] == '' for row in rows)
    assert '12345.67' not in target.read_text(encoding='utf-8')
    assert source.read_bytes() == before
    assert 'CSV created' in capsys.readouterr().out


def test_existing_csv_is_never_overwritten(tmp_path, capsys):
    source = write_fixture(tmp_path)
    target = tmp_path / 'owner.csv'
    target.write_bytes(b'existing owner notes')
    before = source.read_bytes()
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 1
    assert target.read_bytes() == b'existing owner notes'
    assert source.read_bytes() == before
    assert 'already exists' in capsys.readouterr().err


@pytest.mark.parametrize('bad', [
    '{"private":"RAW_PRIVATE",',
    '{"secret":"RAW_PRIVATE","secret":"second"}',
    '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}',
    '[]', 'null',
])
def test_invalid_json_never_creates_export_or_leaks_payload(tmp_path, capsys, bad):
    path = tmp_path / f'state-{DAY}.json'
    path.write_text(bad, encoding='utf-8')
    target = tmp_path / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 1
    assert not target.exists()
    captured = capsys.readouterr()
    assert 'invalid or incompatible' in captured.err
    assert 'RAW_PRIVATE' not in captured.err + captured.out
    assert path.read_text(encoding='utf-8') == bad


@pytest.mark.parametrize('mutation', [
    lambda state: state.update(extra=float('inf')),
    lambda state: state.update(version='old-private-version'),
    lambda state: state['observations'][0].update(outcome_status='win'),
    lambda state: state['requests']['current'].update(failed=100),
    lambda state: state['deliveries'].pop(),
])
def test_corrupt_state_never_creates_export(tmp_path, capsys, mutation):
    state = fixture_state()
    mutation(state)
    path = write_fixture(tmp_path, state)
    if 'Infinity' in path.read_text(encoding='utf-8'):
        path.write_text(path.read_text(encoding='utf-8').replace('Infinity', '1e999'), encoding='utf-8')
    target = tmp_path / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 1
    assert not target.exists()
    assert 'old-private-version' not in capsys.readouterr().err


def test_missing_session_does_not_trigger_legacy_migration(tmp_path, capsys):
    legacy = tmp_path / 'bb-events'
    legacy.mkdir()
    (legacy / f'bb-events-{DAY}.json').write_text('{}', encoding='utf-8')
    directory = tmp_path / 'market-state'
    target = tmp_path / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(directory), '--csv', str(target)]) == 1
    assert not directory.exists() and not target.exists()
    assert 'Cannot read' in capsys.readouterr().err


def test_empty_valid_session_has_zero_counts_and_header_only_export(tmp_path, capsys):
    write_fixture(tmp_path, new_state(DAY))
    target = tmp_path / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 0
    assert list(csv.DictReader(io.StringIO(target.read_text(encoding='utf-8')))) == []
    assert 'Recorded observations: 0' in capsys.readouterr().out


@pytest.mark.parametrize('day', ['20260908', '2026-02-30', '../secrets', '2026-9-8'])
def test_date_format_rejected_without_files(tmp_path, capsys, day):
    with pytest.raises(SystemExit) as exc:
        review.main([day, '--directory', str(tmp_path), '--csv', str(tmp_path / 'owner.csv')])
    assert exc.value.code == 2
    assert not list(tmp_path.iterdir())
    assert 'YYYY-MM-DD' in capsys.readouterr().err


def test_invalid_output_path_is_clean_error_and_does_not_make_directories(tmp_path, capsys):
    path = write_fixture(tmp_path)
    original = path.read_bytes()
    target = tmp_path / 'missing' / 'owner.csv'
    assert review.main([str(DAY), '--directory', str(tmp_path), '--csv', str(target)]) == 1
    assert not target.parent.exists()
    assert 'Cannot create' in capsys.readouterr().err
    assert path.read_bytes() == original


def test_import_and_help_do_not_reach_config_transport_or_history():
    script = '''
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('config', 'research', 'discord', 'yfinance', 'thetadata', 'aiohttp') or fullname.startswith(('core.theta', 'market_state.cog')):
            raise AssertionError('Forbidden review import: ' + fullname)
sys.meta_path.insert(0, Guard())
from market_state.review import main
main(['--help'])
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '--csv PATH' in result.stdout and 'session_date' in result.stdout


def test_actual_module_cli(tmp_path):
    path = write_fixture(tmp_path)
    before = path.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'market_state.review', str(DAY),
                             '--directory', str(tmp_path)], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'Recorded observations: 8' in result.stdout
    assert path.read_bytes() == before
