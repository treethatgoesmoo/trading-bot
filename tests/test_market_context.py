import copy
from datetime import date, timedelta
import json

import pytest

from core.session_bars import new_record, session_time
from spy_setup.market_context import build_context, context_text

DAY = date(2026, 9, 15)
EARLY_CLOSE = date(2026, 11, 27)


def record(count, *, day=DAY, closes=None):
    result = new_record(day)
    opening = session_time(day, 9, 30)
    for index in range(count):
        value = closes[index] if closes is not None else 100 + index / 10
        stamp = opening + timedelta(minutes=index)
        result['bars'].append(dict(timestamp=stamp.isoformat(),
            available_at=(stamp + timedelta(minutes=1, seconds=5)).isoformat(),
            open=value, high=value + 1, low=value - 1, close=value, volume=100))
    return result


def at(minutes, *, seconds=5, day=DAY):
    return session_time(day, 9, 30) + timedelta(minutes=minutes, seconds=seconds)


@pytest.mark.parametrize('minutes,counts,statuses', [
    (0, (0, 0), ('warming_up', 'warming_up')),
    (9, (1, 0), ('warming_up', 'warming_up')),
    (104, (20, 10), ('warming_up', 'warming_up')),
    (105, (21, 10), ('ready', 'warming_up')),
    (209, (41, 20), ('ready', 'warming_up')),
    (210, (42, 21), ('ready', 'ready')),
])
def test_bb_readiness_needs_21_aligned_candles(minutes, counts, statuses):
    context = build_context(record(minutes), at(minutes))
    assert context['available']
    assert tuple(item['candles'] for item in context['timeframes'].values()) == counts
    assert tuple(item['bb_status'] for item in context['timeframes'].values()) == statuses
    assert all(item['bb_required'] == 21 for item in context['timeframes'].values())
    assert context['timeframes']['5']['first_ready_at'] == at(105, seconds=0).isoformat()
    assert context['timeframes']['10']['first_ready_at'] == at(210, seconds=0).isoformat()


@pytest.mark.parametrize('minutes,directions', [
    (9, ('warming_up', 'warming_up')),
    (10, ('up', 'warming_up')),
    (19, ('up', 'warming_up')),
    (20, ('up', 'up')),
])
def test_momentum_needs_two_candles_not_bb_warmup(minutes, directions):
    context = build_context(record(minutes), at(minutes))
    assert tuple(item['momentum'] for item in context['timeframes'].values()) == directions
    assert all(item['bb_status'] == 'warming_up' for item in context['timeframes'].values())


def test_momentum_uses_completed_blocks_not_rolling_or_partial_windows():
    data = record(29)
    data['bars'][-1].update(open=500, high=501, low=499, close=500)
    context = build_context(data, at(29))
    five, ten = context['timeframes']['5'], context['timeframes']['10']
    assert five['change'] == pytest.approx(0.5)
    assert ten['change'] == pytest.approx(1.0)
    assert five['as_of'] == at(25, seconds=0).isoformat()
    assert ten['as_of'] == at(20, seconds=0).isoformat()
    assert context['as_of'] == at(29, seconds=0).isoformat()


@pytest.mark.parametrize('closes,summary,arrows', [
    ([100 + index / 10 for index in range(30)], 'up', ('↑', '↑')),
    ([100 - index / 10 for index in range(30)], 'down', ('↓', '↓')),
    ([100] * 30, 'flat', ('→', '→')),
    ([100] * 10 + [99] * 10 + [98] * 5 + [98.5] * 5, 'mixed', ('↑', '↓')),
    ([100] * 10 + [99] * 10 + [98.5] * 10, 'mixed', ('→', '↓')),
])
def test_direction_and_mixed_summary(closes, summary, arrows):
    context = build_context(record(30, closes=closes), at(30))
    text = context_text(context)
    assert context['momentum'] == summary
    assert f'5m {arrows[0]}' in text and f'10m {arrows[1]}' in text
    assert ('MIXED' in text) == (summary == 'mixed')
    assert 'completed closes' in text


def test_tiny_nonzero_move_does_not_render_zero():
    context = build_context(record(20, closes=[100] * 10 + [100.00001] * 10), at(20))
    text = context_text(context)
    assert '10m ↑ (+0.00)' not in text
    assert '10m ↑ (' in text


@pytest.mark.parametrize('seconds,available', [(90, True), (91, False)])
def test_stale_gate_uses_candle_age_not_fetch_time(seconds, available):
    data = record(210)
    data['last_success_at'] = at(210, seconds=seconds).isoformat()
    # A newly received but old candle must not revive stale data.
    data['bars'][-1]['available_at'] = at(210, seconds=seconds).isoformat()
    context = build_context(data, at(210, seconds=seconds))
    assert context['available'] is available
    if not available:
        assert context['reason'] == 'stale_data'
        assert all(item['change'] is None and item['candles'] is None
                   for item in context['timeframes'].values())
        assert '↑' not in context_text(context)
        assert 'unavailable (stale data)' in context_text(context)


@pytest.mark.parametrize('minutes,seconds,available', [(0, 0, True), (2, 30, True), (2, 31, False)])
def test_empty_startup_record_warms_up_then_becomes_stale(minutes, seconds, available):
    context = build_context(record(0), at(minutes, seconds=seconds))
    assert context['available'] is available
    if available:
        assert '5m warming up (0/2)' in context_text(context)
        assert '5m warming up (0/21 candles)' in context_text(context)


@pytest.mark.parametrize('missing', [0, 7, 103])
def test_missing_session_prefix_minute_fails_closed(missing):
    data = record(105)
    del data['bars'][missing]
    context = build_context(data, at(105))
    assert context['reason'] == 'incomplete_session'
    assert not context['available']
    assert all(item['bb_status'] == 'unavailable' for item in context['timeframes'].values())


def test_future_receipt_cannot_supply_a_completed_block():
    data = record(105)
    data['bars'][-1]['available_at'] = at(106).isoformat()
    context = build_context(data, at(105))
    assert context['available']
    assert context['timeframes']['5']['candles'] == 20
    assert context['timeframes']['5']['bb_status'] == 'warming_up'
    assert build_context(data, at(106))['timeframes']['5']['bb_status'] == 'ready'


def test_future_receipt_inside_prefix_is_a_gap_not_sort_or_fill():
    data = record(105)
    data['bars'][50]['available_at'] = at(106).isoformat()
    assert build_context(data, at(105))['reason'] == 'incomplete_session'


def test_future_candles_are_filtered_even_if_record_contains_them():
    context = build_context(record(110), at(105))
    assert context['timeframes']['5']['candles'] == 21
    assert context['as_of'] == at(105, seconds=0).isoformat()


@pytest.mark.parametrize('corrupt', ['duplicate', 'nan', 'receipt', 'schema', 'missing_key'])
def test_invalid_records_fail_closed(corrupt):
    data = record(30)
    if corrupt == 'duplicate':
        data['bars'].append(copy.deepcopy(data['bars'][-1]))
    elif corrupt == 'nan':
        data['bars'][-1]['close'] = float('nan')
    elif corrupt == 'receipt':
        data['bars'][-1]['available_at'] = 'not-a-date'
    elif corrupt == 'schema':
        data['schema'] = True
    else:
        del data['bars'][-1]['close']
    assert build_context(data, at(30))['reason'] == 'invalid_record'


@pytest.mark.parametrize('data,now,reason', [
    (None, at(30), 'invalid_record'),
    ({}, at(30), 'invalid_record'),
    (record(0), 'bad-clock', 'invalid_record'),
    (record(30), at(30) + timedelta(days=1), 'wrong_session'),
    (record(0), at(-1), 'market_closed'),
    (record(390), at(390, seconds=0), 'market_closed'),
    (record(0, day=date(2026, 9, 19)), at(30, day=date(2026, 9, 19)), 'market_closed'),
])
def test_invalid_clock_session_and_closed_market(data, now, reason):
    assert build_context(data, now)['reason'] == reason


@pytest.mark.parametrize('minutes', [0, 105, 209])
def test_ten_minute_bb_unavailable_for_entire_early_close_session(minutes):
    context = build_context(record(minutes, day=EARLY_CLOSE), at(minutes, day=EARLY_CLOSE))
    assert context['available']
    assert context['timeframes']['10']['bb_status'] == 'session_too_short'
    assert '10m unavailable this session' in context_text(context)
    if minutes >= 105:
        assert context['timeframes']['5']['bb_status'] == 'ready'
        assert context['timeframes']['10']['momentum'] == 'up'


def test_early_close_is_unavailable_at_close_not_ready():
    context = build_context(record(210, day=EARLY_CLOSE), at(210, seconds=0, day=EARLY_CLOSE))
    assert context['reason'] == 'market_closed'


def test_pure_json_compatible_and_no_mutation():
    data = record(210)
    before = copy.deepcopy(data)
    first = build_context(data, at(210))
    assert first == build_context(data, at(210))
    assert json.loads(json.dumps(first, allow_nan=False)) == first
    text = context_text(first)
    assert '5m ready | 10m ready' in text
    assert data == before
    assert len(text) < 1024
