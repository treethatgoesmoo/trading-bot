"""Offline deterministic contracts for the replacement indicator engine."""
from copy import deepcopy
from datetime import date, timedelta
import json
import math

import pytest

from core import market_calendar
from zero_dte.orb_audit import new_record, session_time
from zero_dte.bb_signals import VERSION, MIN_HISTORY, build_snapshot, preceding_sessions
from zero_dte.bb_signal_embeds import snapshot_embed

DAY = date(2026, 9, 8)  # Tuesday after Labor Day; prior trading session is Friday.
PRIOR = date(2026, 9, 4)


def bars(day, values, *, ranges=None):
    opening = session_time(day, 9, 30)
    result = []
    for i, value in enumerate(values):
        stamp = opening + timedelta(minutes=i)
        high, low = ranges[i] if ranges else (value + .1, value - .1)
        result.append(dict(timestamp=stamp.isoformat(), available_at=(stamp + timedelta(minutes=1)).isoformat(),
                           open=value, high=high, low=low, close=value))
    return result


def record(values, day=DAY, ranges=None):
    result = new_record(day)
    result['bars'] = bars(day, values, ranges=ranges)
    return result


def evaluate(values, *, ranges=None, prior=None):
    state = record(values, ranges=ranges)
    now = session_time(DAY, 9, 30) + timedelta(minutes=len(values), seconds=2)
    return build_snapshot(state, prior or [], now)


def oscillating(count):
    return [100 + math.sin(i * .8) for i in range(count)]


@pytest.mark.parametrize('side,direction', [('upper', 1), ('lower', -1)])
def test_contact_outside_continuation_rejection_symmetric(side, direction):
    base = oscillating(40)
    first = evaluate(base + [100 + direction * 8])
    kinds = first['timeframes']['1']['events']
    assert f'{side}_touch' in kinds
    assert f'{side}_close_outside' in kinds
    second = evaluate(base + [100 + direction * 8, 100 + direction * 12])
    assert second['timeframes']['1']['events'] == [f'{side}_continuation']
    third = evaluate(base + [100 + direction * 8, 100 + direction * 12, 100 + direction * 16])
    assert third['timeframes']['1']['events'] == []
    returned = evaluate(base + [100 + direction * 8, 100])
    assert f'{side}_rejection' in returned['timeframes']['1']['events']
    assert f'{side}_close_outside' not in returned['timeframes']['1']['events']


@pytest.mark.parametrize('side', ['upper', 'lower'])
def test_wick_touch_and_inside_close_is_rejection_and_rearms(side):
    values = oscillating(40) + [100]
    ranges = [(v + .1, v - .1) for v in values]
    ranges[-1] = (110, 99.9) if side == 'upper' else (100.1, 90)
    snapshot = evaluate(values, ranges=ranges)
    assert snapshot['timeframes']['1']['events'] == [f'{side}_touch', f'{side}_rejection']
    # Consecutive wick contacts are not new events.
    repeated = evaluate(values + [100], ranges=ranges + [ranges[-1]])
    assert repeated['timeframes']['1']['events'] == []
    # One no-contact candle resets the contact detector.
    reset = evaluate(values + [100, 100], ranges=ranges + [(100.1, 99.9), ranges[-1]])
    assert f'{side}_touch' in reset['timeframes']['1']['events']


def test_flat_prices_are_json_safe_and_do_not_touch_both_bands():
    snapshot = evaluate([100.] * 40)
    frame = snapshot['timeframes']['1']
    assert frame['available'] and frame['rsi'] is None  # shared RSI has 0/0 on a flat series
    assert frame['state'] == 'flat_band'
    assert snapshot['events'] == []
    json.dumps(snapshot, allow_nan=False)


def test_warmup_is_per_timeframe_and_complete_prior_history_is_optional():
    snapshot = evaluate(oscillating(40))
    assert snapshot['fresh']
    assert snapshot['timeframes']['1']['available']
    assert snapshot['timeframes']['5']['reason'] == 'warming_up'
    assert snapshot['timeframes']['15']['history_bars'] == 2
    assert snapshot['timeframes']['15']['required_bars'] == MIN_HISTORY
    prior = bars(PRIOR, oscillating(390))
    warmed = evaluate(oscillating(135), prior=prior)
    assert all(frame['available'] for frame in warmed['timeframes'].values())
    short = evaluate(oscillating(135), prior=prior[:-1])
    assert not short['prior_complete']
    assert not short['timeframes']['15']['available']


def test_two_prior_sessions_ready_at_first_15m_close_and_skip_holiday():
    older = date(2026, 9, 3)
    assert preceding_sessions(DAY) == [PRIOR, older]
    prior = bars(PRIOR, oscillating(390)) + bars(older, oscillating(390))
    before = evaluate(oscillating(14), prior=prior)
    assert before['timeframes']['15']['reason'] == 'no_completed_candle'
    ready = evaluate(oscillating(15), prior=prior)
    assert ready['prior_days'] == [older.isoformat(), PRIOR.isoformat()]
    assert all(frame['available'] for frame in ready['timeframes'].values())
    assert ready['timeframes']['15']['bar_at'] == session_time(DAY, 9, 30).isoformat()
    # No accidental merging across session boundaries; precisely 26+26+1 closes.
    import pandas as pd
    from core.indicators import macd
    values = oscillating(390)[14::15] * 2 + [oscillating(15)[-1]]
    _, _, hist = macd(pd.Series(values, dtype=float))
    assert ready['timeframes']['15']['macd_histogram'] == pytest.approx(hist.iloc[-1])


def test_partial_or_corrupt_older_session_does_not_poison_newer_history():
    older = date(2026, 9, 3)
    recent = bars(PRIOR, oscillating(390))
    baseline = evaluate(oscillating(15), prior=recent)
    assert baseline['prior_days'] == [PRIOR.isoformat()]
    for old in [bars(older, oscillating(389)), bars(older, oscillating(390))]:
        if len(old) == 390:
            old[0]['close'] = float('nan')
        snapshot = evaluate(oscillating(15), prior=recent + old)
        assert snapshot == baseline
    # Cannot reach past a missing immediate session to use a complete older one.
    missing = evaluate(oscillating(15), prior=bars(older, oscillating(390)) + recent[:-1])
    assert missing['prior_days'] == [] and not missing['prior_complete']
    assert missing['timeframes']['15']['history_bars'] == 1


def test_two_session_receipts_are_causal_and_history_is_capped():
    older = date(2026, 9, 3)
    prior = bars(PRIOR, oscillating(390)) + bars(older, oscillating(390))
    now = session_time(DAY, 9, 45)
    for bar in prior:
        if bar['timestamp'].startswith(older.isoformat()):
            bar['available_at'] = (now + timedelta(seconds=1)).isoformat()
    state = record(oscillating(15))
    assert build_snapshot(state, prior, now)['prior_days'] == [PRIOR.isoformat()]
    ready = build_snapshot(state, prior, now + timedelta(seconds=1))
    assert ready['timeframes']['15']['available']
    extra = bars(date(2026, 9, 2), [300.] * 390)
    assert build_snapshot(state, prior + extra, now + timedelta(seconds=1)) == ready


def test_two_sessions_honor_early_close_lengths_and_still_require_35(monkeypatch):
    day = date(2026, 11, 30)
    immediate, older = preceding_sessions(day)
    assert (immediate, older) == (date(2026, 11, 27), date(2026, 11, 25))
    history = bars(immediate, oscillating(210)) + bars(older, oscillating(390))
    state = record(oscillating(15), day)
    now = session_time(day, 9, 45)
    assert build_snapshot(state, history, now)['timeframes']['15']['available']  # 14+26+1
    # Hypothetical consecutive short sessions: complete does not mean enough.
    original = market_calendar.close_time
    monkeypatch.setattr(market_calendar, 'close_time',
                        lambda d: (13, 0) if d in (immediate, older) else original(d))
    short = bars(immediate, oscillating(210)) + bars(older, oscillating(210))
    snapshot = build_snapshot(state, short, now)
    assert len(snapshot['prior_days']) == 2
    assert snapshot['timeframes']['15']['history_bars'] == 29
    assert snapshot['timeframes']['15']['reason'] == 'warming_up'


def test_previous_session_must_be_immediately_previous_trading_session():
    prior = bars(date(2026, 9, 3), oscillating(390))
    snapshot = evaluate(oscillating(135), prior=prior)
    assert snapshot['fresh'] and not snapshot['prior_complete']
    assert not snapshot['timeframes']['15']['available']


def test_session_aligned_completed_buckets_recover_fresh_events_without_lookahead():
    prior = bars(PRIOR, oscillating(390))
    values = oscillating(149) + [120]
    snapshot = evaluate(values, prior=prior)
    frame = snapshot['timeframes']['15']
    assert frame['bar_at'] == session_time(DAY, 11, 45).isoformat()
    assert any(event['timeframe'] == '15' for event in snapshot['events'])
    later = evaluate(values + [130], prior=prior)
    assert later['timeframes']['15'] == frame
    assert {event['id'] for event in snapshot['events'] if event['timeframe'] in ('5', '15')} == {
        event['id'] for event in later['events'] if event['timeframe'] in ('5', '15')}
    # 15m indicators must not see the latest partial 15m candle.
    assert later['timeframes']['15']['close'] == 120
    state = record(values + [130])
    boundary = session_time(DAY, 12)
    at_limit = build_snapshot(state, prior, boundary + timedelta(seconds=90))
    assert any(event['timeframe'] == '15' for event in at_limit['events'])
    expired = build_snapshot(state, prior, boundary + timedelta(seconds=91))
    assert expired['fresh']  # Current 1m evidence cannot extend older event life.
    assert not any(event['timeframe'] in ('5', '15') for event in expired['events'])


def test_future_and_not_yet_received_rows_cannot_change_snapshot():
    state = record(oscillating(40))
    now = session_time(DAY, 10, 10) + timedelta(seconds=2)
    expected = build_snapshot(state, [], now)
    extended = record(oscillating(40) + [999, 888])
    assert build_snapshot(extended, [], now) == expected
    extended['bars'][39]['available_at'] = (now + timedelta(seconds=1)).isoformat()
    delayed = build_snapshot(extended, [], now)
    assert delayed['bar_at'] == session_time(DAY, 10, 8).isoformat()
    assert delayed['timeframes']['1']['close'] != expected['timeframes']['1']['close']


@pytest.mark.parametrize('mutation,reason', [
    ('gap', 'incomplete_session'), ('duplicate', 'invalid_data'),
    ('nan', 'invalid_data'), ('wrong_ohlc', 'invalid_data'),
    ('receipt_before_end', 'invalid_data'), ('no_receipt', 'invalid_data'),
])
def test_bad_current_evidence_fails_closed(mutation, reason):
    state = record(oscillating(40))
    if mutation == 'gap':
        del state['bars'][5]
    elif mutation == 'duplicate':
        state['bars'].append(deepcopy(state['bars'][5]))
    elif mutation == 'nan':
        state['bars'][5]['close'] = float('nan')
    elif mutation == 'wrong_ohlc':
        state['bars'][5]['low'] = 999
    elif mutation == 'receipt_before_end':
        state['bars'][5]['available_at'] = state['bars'][5]['timestamp']
    else:
        del state['bars'][5]['available_at']
    snapshot = build_snapshot(state, [], session_time(DAY, 10, 10))
    assert not snapshot['fresh'] and snapshot['reason'] == reason
    assert snapshot['events'] == []
    assert not any(frame['available'] for frame in snapshot['timeframes'].values())
    json.dumps(snapshot, allow_nan=False)


def test_staleness_measured_from_bar_end_with_inclusive_90_second_limit():
    state = record(oscillating(40))
    end = session_time(DAY, 10, 10)
    assert build_snapshot(state, [], end + timedelta(seconds=90))['fresh']
    late = build_snapshot(state, [], end + timedelta(seconds=91))
    assert late['reason'] == 'stale_data' and late['events'] == []


def test_early_close_and_holiday_are_not_active_sessions():
    day = date(2026, 11, 27)
    assert market_calendar.close_time(day) == (13, 0)
    state = record(oscillating(210), day)
    at_close = build_snapshot(state, [], session_time(day, 13))
    assert at_close['fresh']
    assert at_close['timeframes']['5']['bar_at'] == session_time(day, 12, 55).isoformat()
    assert build_snapshot(state, [], session_time(day, 13, 1))['reason'] == 'market_closed'
    holiday = date(2026, 9, 7)
    assert build_snapshot(record([100], holiday), [], session_time(holiday, 10))['reason'] == 'market_closed'


def test_full_early_close_prior_is_accepted_without_overnight_bucket_merging():
    day = date(2026, 11, 30)
    prior = bars(date(2026, 11, 27), oscillating(210))
    state = record(oscillating(150), day)
    snapshot = build_snapshot(state, prior, session_time(day, 12))
    assert snapshot['prior_complete']
    assert snapshot['timeframes']['15']['history_bars'] == 14 + 10


def test_shared_math_matches_five_minute_completed_closes():
    import pandas as pd
    from core.indicators import bollinger, macd, rsi_series
    values = oscillating(175)
    snapshot = evaluate(values)
    frame = snapshot['timeframes']['5']
    closes = pd.Series(values[4::5], dtype=float)
    upper, middle, lower = bollinger(closes)
    rsi = rsi_series(closes)
    _, _, histogram = macd(closes)
    assert frame['available']
    assert frame['upper'] == pytest.approx(upper.iloc[-1])
    assert frame['middle'] == pytest.approx(middle.iloc[-1])
    assert frame['lower'] == pytest.approx(lower.iloc[-1])
    assert frame['rsi'] == pytest.approx(rsi.iloc[-1])
    assert frame['rsi_change'] == pytest.approx(rsi.iloc[-1] - rsi.iloc[-2])
    assert frame['macd_histogram'] == pytest.approx(histogram.iloc[-1])
    assert frame['macd_change'] == pytest.approx(histogram.iloc[-1] - histogram.iloc[-2])


@pytest.mark.parametrize('direction,expected', [(1, 100.), (-1, 0.)])
def test_monotonic_rsi_remains_finite(direction, expected):
    snapshot = evaluate([100 + direction * i for i in range(40)])
    assert snapshot['timeframes']['1']['rsi'] == expected
    json.dumps(snapshot, allow_nan=False)


def test_first_partial_buckets_have_no_completed_candle():
    snapshot = evaluate(oscillating(4), prior=bars(PRIOR, oscillating(390)))
    assert snapshot['timeframes']['1']['available']
    assert snapshot['timeframes']['5']['reason'] == 'no_completed_candle'
    assert snapshot['timeframes']['15']['reason'] == 'no_completed_candle'


def test_extreme_finite_input_cannot_leak_nonfinite_indicator_values():
    snapshot = evaluate([1e300 + (i % 2) * 1e299 for i in range(40)])
    assert snapshot['timeframes']['1']['reason'] == 'invalid_indicator_values'
    assert snapshot['events'] == []
    json.dumps(snapshot, allow_nan=False)


def test_ids_stable_input_unmodified_and_embed_is_observational():
    state = record(oscillating(40) + [120])
    original = deepcopy(state)
    now = session_time(DAY, 10, 11)
    snapshot = build_snapshot(state, [], now)
    second = build_snapshot(state, [], now + timedelta(seconds=20))
    assert state == original
    assert snapshot['events'] == second['events']
    assert all(event['id'].startswith(VERSION + '/') for event in snapshot['events'])
    embed = snapshot_embed(snapshot).to_dict()
    assert len(embed['fields']) == 3
    assert 'RSI' in embed['fields'][0]['value'] and 'MACD hist' in embed['fields'][0]['value']
    assert 'Observations, not buy/sell signals' in embed['footer']['text']
    assert 'Closed 10:11 ET' in embed['fields'][0]['value']
    expired = deepcopy(snapshot)
    expired.update(fresh=False, reason='stale_data')
    expired_embed = snapshot_embed(expired).to_dict()
    assert 'Data unavailable' in expired_embed['description']
    assert all('Unavailable' in field['value'] for field in expired_embed['fields'])
    json.dumps(snapshot, allow_nan=False)
