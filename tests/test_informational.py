"""Synthetic, offline tests: no provider or Discord access."""
import copy
from datetime import date, timedelta
from statistics import pstdev, mean

import pytest

from core.session_bars import new_record, session_time
from spy_setup.informational import VERSION, build_events

DAY = date(2026, 9, 15)
MINUTE = timedelta(minutes=1)


def record(closes, *, day=DAY, delay=5):
    result = new_record(day)
    opening = session_time(day, 9, 30)
    for index, close in enumerate(closes):
        stamp = opening + index * MINUTE
        result['bars'].append(dict(timestamp=stamp.isoformat(),
                                  available_at=(stamp + MINUTE + timedelta(seconds=delay)).isoformat(),
                                  open=close, high=close + .1, low=close - .1,
                                  close=close, volume=100))
    return result


def at(minutes, seconds=5, *, day=DAY):
    return session_time(day, 9, 30) + minutes * MINUTE + timedelta(seconds=seconds)


def kinds(events):
    return [event['kind'] for event in events]


def bb(events, timeframe):
    return [event for event in events if event['kind'].startswith('bb_')
            and event['timeframe'] == str(timeframe)]


def test_opening_range_ready_close_identity_and_no_mutation():
    data = record([100] * 15)
    before = copy.deepcopy(data)
    events = build_events(data, at(15))
    assert kinds(events) == ['opening_range_ready']
    event = events[0]
    assert event['range_high'] == 100.1
    assert event['range_low'] == 99.9
    assert event['event_at'] == at(15, 0).isoformat()
    assert event['informational'] is True
    assert event['version'] == VERSION
    assert all(part in event['id'] for part in (VERSION, str(DAY), event['kind'], event['event_at']))
    assert build_events(data, at(15, 25)) == events
    assert data == before


@pytest.mark.parametrize('seconds,expected', [(90, True), (91, False)])
def test_freshness_boundary(seconds, expected):
    assert bool(build_events(record([100] * 15), at(15, seconds))) is expected


def test_opening_range_not_early_or_backfilled():
    assert build_events(record([100] * 14), at(14)) == []
    assert build_events(record([100] * 15), at(15, 0)) == []
    data = record([100] * 15)
    data['bars'][0]['available_at'] = at(15, 91).isoformat()
    assert build_events(data, at(15, 91)) == []


@pytest.mark.parametrize('price,direction', [(102, 'above'), (98, 'below')])
def test_range_cross_requires_new_crossing(price, direction):
    data = record([100] * 15 + [price, price, price])
    first = build_events(data, at(16))
    crosses = [e for e in first if e['kind'].startswith('opening_range_cross_')]
    assert len(crosses) == 1
    assert crosses[0]['kind'] == f'opening_range_cross_{direction}'
    assert crosses[0]['close'] == price
    assert build_events(data, at(18)) == []


def test_range_touch_is_not_cross_but_recross_has_new_identity():
    data = record([100] * 15 + [100.1, 102, 100, 102])
    assert not any('cross' in e['kind'] for e in build_events(data, at(16)))
    first = next(e for e in build_events(data, at(17)) if 'cross' in e['kind'])
    again = next(e for e in build_events(data, at(19)) if 'cross' in e['kind'])
    assert first['id'] != again['id']


@pytest.mark.parametrize('timeframe', [5, 10])
@pytest.mark.parametrize('last,direction', [(110, 'above'), (90, 'below')])
def test_bb_population_bands_and_21_bar_requirement(timeframe, last, direction):
    closes = [100] * (21 * timeframe)
    closes[-1] = last
    data = record(closes)
    assert bb(build_events(data, at(20 * timeframe)), timeframe) == []
    found = bb(build_events(data, at(21 * timeframe)), timeframe)
    assert len(found) == 1
    event = found[0]
    sample = [100] * 19 + [last]
    assert event['upper'] == pytest.approx(mean(sample) + 2 * pstdev(sample))
    assert event['lower'] == pytest.approx(mean(sample) - 2 * pstdev(sample))
    assert event['previous_upper'] == event['previous_lower'] == 100
    assert event['kind'] == f'bb_cross_{direction}'
    assert event['period'] == 20 and event['deviations'] == 2
    assert event['event_at'] == at(21 * timeframe, 0).isoformat()


@pytest.mark.parametrize('timeframe', [5, 10])
def test_bb_staying_outside_does_not_repeat(timeframe):
    closes = [price for price in [100] * 20 + [110, 111, 112]
              for _ in range(timeframe)]
    data = record(closes)
    assert len(bb(build_events(data, at(21 * timeframe)), timeframe)) == 1
    assert bb(build_events(data, at(23 * timeframe)), timeframe) == []


@pytest.mark.parametrize('timeframe', [5, 10])
def test_bb_requires_complete_aligned_block(timeframe):
    data = record([100] * (20 * timeframe) + [110] * timeframe)
    assert bb(build_events(data, at(21 * timeframe - 1)), timeframe) == []
    assert len(bb(build_events(data, at(21 * timeframe)), timeframe)) == 1
    assert bb(build_events(data, at(21 * timeframe + 2)), timeframe) == []


def test_returns_all_fresh_events_not_only_latest():
    data = record([100] * 209 + [110])
    events = build_events(data, at(210))
    assert set(kinds(events)) == {'opening_range_cross_above', 'bb_cross_above'}
    assert len({e['id'] for e in events}) == 3
    assert {e['timeframe'] for e in events} == {'1', '5', '10'}
    assert events == sorted(events, key=lambda e: (e['event_at'], e['id']))


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'bad_price', 'bad_receipt', 'other_day'])
def test_invalid_or_incomplete_evidence_fails_closed(damage):
    data = record([100] * 20 + [110])
    if damage == 'missing':
        del data['bars'][4]
    elif damage == 'duplicate':
        data['bars'].insert(4, copy.deepcopy(data['bars'][4]))
    elif damage == 'bad_price':
        data['bars'][4]['close'] = float('nan')
    elif damage == 'bad_receipt':
        data['bars'][4]['available_at'] = at(0).isoformat()
    else:
        data['bars'][4]['timestamp'] = session_time(DAY - timedelta(days=1), 9, 34).isoformat()
    assert build_events(data, at(21)) == []


@pytest.mark.parametrize('timeframe', [5, 10])
def test_receipt_gating_gap_and_late_crossing(timeframe):
    end = 21 * timeframe
    data = record([100] * (end - 1) + [110])
    data['bars'][4]['available_at'] = at(end, 80).isoformat()
    assert build_events(data, at(end)) == []
    assert bb(build_events(data, at(end, 80)), timeframe)
    data['bars'][-1]['available_at'] = at(end, 91).isoformat()
    assert build_events(data, at(end, 91)) == []


@pytest.mark.parametrize('timeframe', [5, 10])
def test_late_previous_crossing_candle_is_not_trusted(timeframe):
    end = 21 * timeframe
    data = record([100] * (end - 1) + [110])
    data['bars'][20 * timeframe - 1]['available_at'] = at(20 * timeframe, 100).isoformat()
    assert bb(build_events(data, at(end, 60)), timeframe) == []


@pytest.mark.parametrize('day', [DAY, date(2026, 11, 27)])
def test_no_postclose_events_including_early_close(day):
    from core.market_calendar import close_time
    closing = session_time(day, *close_time(day))
    count = int((closing - session_time(day, 9, 30)).total_seconds() / 60)
    data = record([100] * (count - 1) + [110], day=day)
    assert build_events(data, closing + timedelta(seconds=5)) == []


def test_nontrading_date_and_wrong_session_are_rejected():
    holiday = date(2026, 12, 25)
    assert build_events(record([100] * 21, day=holiday), at(21, day=holiday)) == []
    assert build_events(record([100] * 20 + [110]), at(21, day=DAY + timedelta(days=1))) == []


@pytest.mark.parametrize('timeframe', [5, 10])
def test_flat_bands_do_not_emit_a_touch(timeframe):
    end = 21 * timeframe
    assert bb(build_events(record([100] * end), at(end)), timeframe) == []


def test_retired_one_minute_and_unsupported_thirty_minute_bb_never_emit():
    data = record([100] * 20 + [110] + [100] * 187 + [110])
    for end in (21, 210):
        events = build_events(data, at(end))
        assert bb(events, 1) == []
        assert bb(events, 30) == []
    assert 'opening_range_cross_above' in kinds(build_events(data, at(21)))


def test_ten_minute_bb_cannot_warm_up_on_early_close():
    day = date(2026, 11, 27)
    data = record([100] * 209 + [110], day=day)
    assert bb(build_events(data, at(209, day=day)), 10) == []
    assert build_events(data, at(210, day=day)) == []
