"""Offline contracts for spx-state-v1, independent of collection and delivery."""
from copy import deepcopy
from datetime import date, timedelta
import json
import math

import pandas as pd
import pytest

from core import market_calendar
from core.indicators import bollinger
from core.session_bars import session_time
from market_state import signals
from market_state.signals import VERSION, build_snapshot, preceding_sessions
from market_state.embeds import snapshot_embed

DAY = date(2026, 9, 8)


def bars(day, values, *, spread=.1):
    opening = session_time(day, 9, 30)
    return [dict(timestamp=(opening + timedelta(minutes=i)).isoformat(),
                 available_at=(opening + timedelta(minutes=i + 1)).isoformat(),
                 open=float(value), high=float(value + spread), low=float(value - spread),
                 close=float(value)) for i, value in enumerate(values)]


def record(values, day=DAY):
    return dict(session_date=day.isoformat(), bars=bars(day, values))


def oscillating(count, offset=0):
    return [5000 + math.sin((i + offset) * .8) for i in range(count)]


def history(day=DAY):
    recent, older = preceding_sessions(day)
    result = []
    for d in (older, recent):
        count = int((session_time(d, *market_calendar.close_time(d)) - session_time(d, 9, 30)).total_seconds() / 60)
        result += bars(d, oscillating(count))
    return result


def evaluate(values, *, prior=None, day=DAY, seconds=2):
    now = session_time(day, 9, 30) + timedelta(minutes=len(values), seconds=seconds)
    return build_snapshot(record(values, day), history(day) if prior is None else prior, now)


def synthetic_frame(direction='bullish', **overrides):
    return dict(available=True, reason='complete', bar_at=session_time(DAY, 9, 30).isoformat(),
                close=101. if direction == 'bullish' else 99., middle=100.,
                rsi=55. if direction == 'bullish' else 45.,
                macd_histogram=1. if direction == 'bullish' else -1., **overrides)


def bucket_rows(count, minutes=5):
    return [dict(timestamp=session_time(DAY, 9, 30) + timedelta(minutes=i * minutes),
                 open=100., high=101., low=99., close=100.) for i in range(count)]


@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
def test_direction_thresholds_are_symmetric_and_context_histogram_can_equal_zero(direction):
    five, fifteen = synthetic_frame(direction), synthetic_frame(direction)
    fifteen.update(rsi=50., macd_histogram=0.)
    assert signals._raw_direction(five, fifteen) == direction
    five['rsi'] += -.001 if direction == 'bullish' else .001
    assert signals._raw_direction(five, fifteen) == 'neutral'


@pytest.mark.parametrize('key,value', [('close', 100.), ('rsi', 54.99), ('macd_histogram', 0.)])
def test_bullish_requires_all_five_minute_conditions(key, value):
    five, fifteen = synthetic_frame(), synthetic_frame()
    five[key] = value
    assert signals._raw_direction(five, fifteen) == 'neutral'


@pytest.mark.parametrize('key,value', [('close', 100.), ('rsi', 49.99), ('macd_histogram', -.001)])
def test_bullish_requires_all_fifteen_minute_conditions(key, value):
    five, fifteen = synthetic_frame(), synthetic_frame()
    fifteen[key] = value
    assert signals._raw_direction(five, fifteen) == 'neutral'


@pytest.mark.parametrize('key,value', [('rsi', None), ('macd_histogram', float('nan')), ('close', float('inf'))])
def test_missing_or_invalid_direction_inputs_are_not_neutral(key, value):
    five, fifteen = synthetic_frame(), synthetic_frame()
    five[key] = value
    assert signals._raw_direction(five, fifteen) == 'unavailable'
    assert signals._raw_direction(dict(five, available=False), fifteen) == 'unavailable'


def test_conflicting_indicators_are_neutral_not_volatile():
    assert signals._raw_direction(synthetic_frame(), synthetic_frame('bearish')) == 'neutral'
    result = signals._volatility_test(1., 1., .01, .01, .01)
    assert result['available'] and result['trigger'] is False


@pytest.mark.parametrize('tr,baseline,width,width_base,previous,expected_range,expected_width', [
    (1.5, 1., .01, .01, .01, True, False),
    (1.499, 1., .01, .01, .01, False, False),
    (1., 1., .015, .01, .014, False, True),
    (1., 1., .015, .01, .015, False, False),
    (1., 1., .015, .01, .016, False, False),
    (1., 1., .01499, .01, .014, False, False),
    (0., 0., 0., 0., 0., False, False),
])
def test_volatility_thresholds_and_expansion(tr, baseline, width, width_base, previous, expected_range, expected_width):
    result = signals._volatility_test(tr, baseline, width, width_base, previous)
    assert result['available']
    assert result['range_trigger'] is expected_range
    assert result['width_trigger'] is expected_width
    assert result['trigger'] is (expected_range or expected_width)


@pytest.mark.parametrize('values', [(1., 0., 0., 0., 0.), (1., 1., .01, 0., 0.)])
def test_positive_movement_against_zero_baseline_is_unavailable(values):
    result = signals._volatility_test(*values)
    assert not result['available'] and result['trigger'] is None
    assert result['reason'] == 'zero_volatility_baseline'


def test_nonfinite_volatility_values_are_json_safe_unavailable():
    result = signals._volatility_test(float('inf'), 1., float('nan'), 1., 1.)
    assert not result['available']
    assert result['true_range'] is None and result['band_width'] is None
    json.dumps(result, allow_nan=False)


def test_volatility_uses_exact_prior_twenty_and_excludes_current():
    rows = bucket_rows(50)
    for i, row in enumerate(rows):
        row.update(close=100 + math.sin(i * .8), high=102 + i * .01, low=98.)
    rows[-1].update(high=120., low=90., close=110.)
    result = signals._raw_volatility(rows)
    prior_ranges = [max(r['high'] - r['low'], abs(r['high'] - rows[i - 1]['close']),
                        abs(r['low'] - rows[i - 1]['close']))
                    for i, r in enumerate(rows) if 29 <= i < 49]
    from statistics import median
    upper, middle, lower = bollinger(pd.Series([r['close'] for r in rows]))
    widths = (upper - lower) / middle
    assert result['range_baseline'] == pytest.approx(median(prior_ranges))
    assert result['width_baseline'] == pytest.approx(widths.iloc[-21:-1].median())
    assert result['true_range'] == 30.
    assert result['range_trigger'] is True
    assert result['range_baseline'] != pytest.approx(median(prior_ranges[1:] + [30.]))


def test_volatility_warmup_needs_twenty_valid_prior_bandwidths():
    assert signals._raw_volatility(bucket_rows(39))['reason'] == 'volatility_warming_up'
    ready = signals._raw_volatility(bucket_rows(40))
    assert ready['available'] and ready['trigger'] is False


@pytest.mark.parametrize('previous,current,expected', [
    ('bullish', 'bullish', 'bullish'), ('bearish', 'bearish', 'bearish'),
    ('neutral', 'bullish', 'neutral'), ('bullish', 'bearish', 'neutral'),
    ('bearish', 'neutral', 'neutral'),
])
def test_direction_requires_two_consecutive_current_session_five_minute_candles(monkeypatch, previous, current, expected):
    five, fifteen = bucket_rows(4), bucket_rows(1, 15)
    monkeypatch.setattr(signals, '_frame', lambda rows: synthetic_frame())
    directions = iter([previous, current])
    monkeypatch.setattr(signals, '_raw_direction', lambda a, b: next(directions))
    monkeypatch.setattr(signals, '_raw_volatility', lambda rows: dict(
        available=True, trigger=False, range_trigger=False, width_trigger=False))
    result = signals._classification(five, fifteen, [], [])
    assert result['available'] and result['direction'] == expected
    assert result['volatile'] is False
    # Yesterday's candles cannot supply confirmation even with warmed math.
    result = signals._classification(five[:1], [], bucket_rows(50), bucket_rows(50, 15))
    assert not result['available'] and result['reason'] == 'need_two_current_session_5m_candles'


@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
@pytest.mark.parametrize('previous,current,expected', [(False, True, True), (True, False, True), (False, False, False)])
def test_volatility_is_independent_and_clears_after_two_quiet_candles(monkeypatch, previous, current, expected, direction):
    monkeypatch.setattr(signals, '_frame', lambda rows: synthetic_frame(direction))
    triggers = iter([previous, current])
    def volatility(rows):
        trigger = next(triggers)
        return dict(available=True, trigger=trigger, range_trigger=trigger, width_trigger=False)
    monkeypatch.setattr(signals, '_raw_volatility', volatility)
    result = signals._classification(bucket_rows(4), bucket_rows(1, 15), [], [])
    assert result['direction'] == direction
    assert result['volatile'] is expected


def test_fifteen_minute_context_is_causal_for_each_confirmation_candle(monkeypatch):
    def frame(rows):
        result = synthetic_frame()
        result['bar_at'] = rows[-1]['timestamp'].isoformat()
        return result
    monkeypatch.setattr(signals, '_frame', frame)
    monkeypatch.setattr(signals, '_raw_volatility', lambda rows: dict(
        available=True, trigger=False, range_trigger=False, width_trigger=False))
    result = signals._classification(bucket_rows(6), bucket_rows(2, 15), [], [])
    assert result['raw_current']['context_bar_at'] == session_time(DAY, 9, 45).isoformat()
    assert result['raw_previous']['context_bar_at'] == session_time(DAY, 9, 30).isoformat()
    # At09:45 the prior5m ended09:40; today's first15m must not leak into it.
    result = signals._classification(bucket_rows(3), bucket_rows(1, 15), [], [])
    assert not result['available']
    assert result['raw_previous']['context_bar_at'] is None


@pytest.mark.parametrize('sign,expected', [(1, 'bullish'), (-1, 'bearish')])
def test_real_indicator_pipeline_establishes_symmetric_direction(sign, expected):
    older, recent = reversed(preceding_sessions(DAY))
    values = [5000 + sign * .0001 * i * i for i in range(800)]
    prior = bars(older, values[:390]) + bars(recent, values[390:780])
    result = evaluate(values[780:], prior=prior)
    assert result['version'] == VERSION
    assert result['classification']['available']
    assert result['classification']['direction'] == expected
    assert result['classification']['bar_at'] == session_time(DAY, 9, 45).isoformat()
    json.dumps(result, allow_nan=False)


def test_current_session_fifteen_minute_context_delays_first_classification_to_0950():
    before = evaluate(oscillating(15))
    assert all(f['available'] for f in before['timeframes'].values())
    assert not before['classification']['available']
    assert before['classification']['raw_previous']['context_bar_at'] is None
    ready = evaluate(oscillating(20))
    assert ready['classification']['available']
    assert ready['classification']['raw_previous']['context_bar_at'] == session_time(DAY, 9, 30).isoformat()


def test_partial_five_minute_and_one_minute_changes_do_not_reclassify():
    values = oscillating(30)
    baseline = evaluate(values)
    partial = evaluate(values + [5100])
    assert partial['fresh']
    assert partial['timeframes']['1']['close'] == 5100
    assert partial['classification'] == baseline['classification']
    # Latest5m is legitimate status context >90s after close, NOT fresh for send.
    later = evaluate(values + [5100, 5101])
    assert later['fresh'] and later['classification'] == baseline['classification']
    end = session_time(DAY, 10, 0)
    assert (signals.utc(later['as_of']) - end).total_seconds() > 90


def test_future_and_unreceived_bars_cannot_change_snapshot():
    state = record(oscillating(31))
    now = session_time(DAY, 10, 0) + timedelta(seconds=2)
    baseline = build_snapshot(dict(state, bars=state['bars'][:30]), history(), now)
    assert build_snapshot(state, history(), now) == baseline
    state['bars'][-1]['timestamp'] = session_time(DAY, 9, 59).isoformat()
    state['bars'][-1]['available_at'] = (now + timedelta(seconds=1)).isoformat()
    assert build_snapshot(state, history(), now) == baseline


def test_gaps_duplicates_bad_prices_and_impossible_receipts_fail_closed():
    state = record(oscillating(30))
    now = session_time(DAY, 10, 0) + timedelta(seconds=2)
    missing = deepcopy(state)
    del missing['bars'][2]
    assert build_snapshot(missing, history(), now)['reason'] == 'incomplete_session'
    duplicate = deepcopy(state)
    duplicate['bars'].append(duplicate['bars'][2])
    assert build_snapshot(duplicate, history(), now)['reason'] == 'invalid_data'
    for key, value in [('close', float('nan')), ('high', 1.), ('available_at', session_time(DAY, 9, 30).isoformat())]:
        bad = deepcopy(state)
        bad['bars'][0][key] = value
        result = build_snapshot(bad, history(), now)
        assert not result['fresh'] and result['classification']['direction'] == 'unavailable'
        json.dumps(result, allow_nan=False)


def test_latest_minute_freshness_boundaries_and_session_end():
    state = record(oscillating(30))
    end = session_time(DAY, 10, 0)
    assert build_snapshot(state, history(), end + timedelta(seconds=90))['fresh']
    stale = build_snapshot(state, history(), end + timedelta(seconds=91))
    assert stale['reason'] == 'stale_data' and stale['classification']['volatile'] is None
    assert build_snapshot(state, history(), session_time(DAY, 16, 1))['reason'] == 'market_closed'
    assert build_snapshot(state, history(), session_time(DAY, 9, 29))['reason'] == 'market_closed'


def test_flat_rsi_is_unavailable_not_fabricated_neutral():
    prior = [b for d in preceding_sessions(DAY) for b in bars(d, [5000.] * 390)]
    result = evaluate([5000.] * 30, prior=prior)
    assert result['timeframes']['5']['rsi'] is None
    assert result['classification']['direction'] == 'unavailable'
    assert result['classification']['volatile'] is None
    json.dumps(result, allow_nan=False)


def test_optional_prior_history_is_contiguous_chain_and_receipt_causal():
    recent, older = preceding_sessions(DAY)
    assert (recent, older) == (date(2026, 9, 4), date(2026, 9, 3))
    recent_bars, older_bars = bars(recent, oscillating(390)), bars(older, oscillating(390))
    missing_recent = evaluate(oscillating(30), prior=recent_bars[:-1] + older_bars)
    assert missing_recent['prior_days'] == []
    bad_older = deepcopy(older_bars)
    bad_older[0]['close'] = float('nan')
    result = evaluate(oscillating(30), prior=recent_bars + bad_older)
    assert result['prior_days'] == [recent.isoformat()]
    now = session_time(DAY, 10, 0) + timedelta(seconds=2)
    for row in older_bars:
        row['available_at'] = (now + timedelta(seconds=1)).isoformat()
    before = build_snapshot(record(oscillating(30)), recent_bars + older_bars, now)
    after = build_snapshot(record(oscillating(30)), recent_bars + older_bars, now + timedelta(seconds=1))
    assert before['prior_days'] == [recent.isoformat()]
    assert after['prior_days'] == [older.isoformat(), recent.isoformat()]
    assert not before['classification']['available'] and after['classification']['available']


def test_early_close_history_and_no_buckets_cross_sessions():
    day = date(2026, 11, 30)
    result = evaluate(oscillating(20), day=day)
    assert preceding_sessions(day) == [date(2026, 11, 27), date(2026, 11, 25)]
    assert result['classification']['available']
    prior_values = oscillating(390)[14::15] + oscillating(210)[14::15]
    current_values = [oscillating(20)[14]]
    from core.indicators import macd
    _, _, histogram = macd(pd.Series(prior_values + current_values))
    assert result['timeframes']['15']['macd_histogram'] == pytest.approx(histogram.iloc[-1])


def test_snapshot_does_not_mutate_inputs_and_all_nested_numbers_are_json_safe():
    state, prior = record(oscillating(30)), history()
    before = deepcopy((state, prior))
    result = build_snapshot(state, prior, session_time(DAY, 10, 0))
    assert (state, prior) == before
    json.dumps(result, allow_nan=False)
    enormous = evaluate([1e308 if i % 2 else 1e-308 for i in range(30)])
    assert not enormous['classification']['available']
    json.dumps(enormous, allow_nan=False)


def test_embed_explains_completed_state_and_independent_volatility_without_trade_advice():
    result = evaluate(oscillating(30))
    result['classification'].update(available=True, direction='bullish', volatile=True,
                                    reasons=['Two completed 5m candles align.'])
    embed = snapshot_embed(result).to_dict()
    assert 'BULLISH + VOLATILE' in embed['title']
    assert '10:00 ET' in embed['description']
    assert '1m • context only' == embed['fields'][0]['name']
    assert VERSION in embed['footer']['text']
    assert 'Observations, not predictions' in embed['footer']['text']
    result['classification'].update(direction='neutral', volatile=False)
    result['transition'] = dict(previous_direction='bullish')
    assert 'bias cleared' in snapshot_embed(result).title


def test_stale_embed_cannot_inherit_bullish_or_volatile_title():
    result = evaluate(oscillating(30))
    result['classification'].update(available=True, direction='bullish', volatile=True)
    result.update(fresh=False, reason='stale_data')
    embed = snapshot_embed(result)
    assert embed.title == 'SPX • State unavailable'
    assert 'stale data' in embed.description
    assert all('Unavailable' in field.value for field in embed.fields)


def test_embed_operational_metadata_is_bounded_and_does_not_dump_arbitrary_fields():
    result = evaluate(oscillating(30))
    result['operational'] = dict(health='warming_up', collection_enabled=True,
                                 publication_enabled=False, secret='not for output')
    embed = snapshot_embed(result)
    service = embed.fields[-1]
    assert service.name == 'Service' and 'publication enabled: False' in service.value
    assert 'not for output' not in service.value
