from datetime import date, timedelta
import copy
import json

import pytest

from core.session_bars import new_record, session_time
from spy_setup.signals import build_snapshot, preceding_sessions, state_key, VERSION
from spy_setup.embeds import snapshot_embed

DAY = date(2026, 9, 8)


def bar(day, minute, o=100, h=101, l=99, c=100, volume=100, receipt=None):
    stamp = session_time(day, 9, 30) + timedelta(minutes=minute)
    return dict(timestamp=stamp.isoformat(), available_at=(receipt or stamp + timedelta(minutes=1)).isoformat(),
                open=o, high=h, low=l, close=c, volume=volume)


def history(count=5, receipt=None):
    return [bar(day, minute, receipt=receipt) for day in preceding_sessions(DAY)[:count] for minute in range(390)]


def record(extra=()):
    return dict(new_record(DAY), bars=[bar(DAY, i) for i in range(15)] + list(extra))


def breakout():
    return bar(DAY, 15, 100, 101.5, 100, 101.3, 200)


def retest():
    return bar(DAY, 16, 101.3, 101.4, 101, 101.2, 100)


def trigger():
    return bar(DAY, 17, 101.2, 101.7, 101.1, 101.6, 200)


def snapshot(extra=(), prior=None, now=None):
    rec = record(extra)
    from core.session_bars import utc
    return build_snapshot(rec, history() if prior is None else prior,
                          now or utc(rec['bars'][-1]['timestamp']) + timedelta(minutes=1, seconds=5))


def test_full_lifecycle_and_close_entry_reference():
    first = snapshot([breakout()])
    retested = snapshot([breakout(), retest()])
    active = snapshot([breakout(), retest(), trigger()])
    assert first['classification']['phase'] == 'breakout'
    assert first['classification']['trigger'] is None
    assert retested['classification']['phase'] == 'retest'
    assert retested['classification']['trigger'] == pytest.approx(101.41)
    c = active['classification']
    assert c['lifecycle'] == 'triggered'
    assert c['entry_reference'] == 101.6
    assert c['invalidation'] == pytest.approx(100.99)
    assert c['target'] == pytest.approx(102.82)
    assert len(active['events']) == 3
    assert len({json.dumps(state_key(s['classification']), sort_keys=True) for s in [first, retested, active]}) == 3


def test_invalidation_wins_ambiguous_post_entry_candle():
    c = snapshot([breakout(), retest(), trigger(), bar(DAY, 18, 101.6, 104, 100.5, 103)])['classification']
    assert c['lifecycle'] == 'invalidated'


def test_no_same_trigger_candle_target_fill():
    huge = trigger()
    huge['high'] = 110
    assert snapshot([breakout(), retest(), huge])['classification']['lifecycle'] == 'triggered'


def test_target_and_no_same_direction_reentry():
    extra = [breakout(), retest(), trigger(), bar(DAY, 18, 101.6, 103, 101.5, 102.9),
             bar(DAY, 19), bar(DAY, 20, 100, 102, 100, 101.5, 200)]
    snap = snapshot(extra)
    assert snap['classification']['lifecycle'] == 'target_reached'
    assert len(snap['events']) == 4
    assert snap['classification']['event_at'] == (session_time(DAY, 9, 49)).isoformat()


def test_symmetric_put_setup():
    def invert(row):
        row = dict(row)
        row.update(open=200-row['open'], high=200-row['low'], low=200-row['high'], close=200-row['close'])
        return row
    c = snapshot([invert(row) for row in [breakout(), retest(), trigger()]])['classification']
    assert c['direction'] == 'bearish'
    assert c['option_side'] == 'PUT'
    assert c['entry_reference'] == pytest.approx(98.4)
    assert c['target'] == pytest.approx(97.18)


def test_requires_three_same_slot_samples_and_volume_confirmation():
    assert snapshot([breakout()], prior=history(2))['reason'] == 'volume_baseline_unavailable'
    assert snapshot([breakout()], prior=history(3))['classification']['phase'] == 'breakout'
    small = breakout()
    small['volume'] = 110
    assert snapshot([small])['classification']['lifecycle'] == 'idle'


def test_later_history_does_not_retroactively_create_setup():
    late = session_time(DAY, 9, 48)
    extra = [breakout(), retest(), trigger()]
    c = snapshot(extra, prior=history(receipt=late))['classification']
    assert c['lifecycle'] == 'idle'


def test_history_excludes_nonpreceding_sessions_and_duplicate_day():
    prior = history(2) + [bar(date(2026, 8, 10), i) for i in range(390)]
    assert snapshot([breakout()], prior=prior)['reason'] == 'volume_baseline_unavailable'
    prior = history(3)
    prior.append(dict(prior[0]))
    assert snapshot([breakout()], prior=prior)['reason'] == 'volume_baseline_unavailable'


def test_completed_and_receipt_gated_no_mutation_json_safe():
    extra = [breakout(), retest(), trigger()]
    rec, prior = record(extra), history()
    before = copy.deepcopy((rec, prior))
    now = session_time(DAY, 9, 47, ) + timedelta(seconds=30)
    snap = build_snapshot(rec, prior, now)
    assert snap['classification']['phase'] == 'retest'
    assert (rec, prior) == before
    json.dumps(snap, allow_nan=False)


@pytest.mark.parametrize('bad_volume', [None, -1, float('nan'), True])
def test_bad_volume_fails_closed(bad_volume):
    row = breakout()
    row['volume'] = bad_volume
    assert snapshot([row])['reason'] == 'invalid_data'


def test_missing_stale_and_future_receipts_are_not_neutral():
    rec = record([breakout(), retest()])
    rec['bars'].pop(3)
    assert build_snapshot(rec, history(), session_time(DAY, 9, 47))['reason'] == 'incomplete_session'
    assert snapshot([breakout()], now=session_time(DAY, 9, 48))['reason'] == 'stale_data'
    row = breakout()
    row['available_at'] = session_time(DAY, 10).isoformat()
    assert snapshot([row], now=session_time(DAY, 9, 46))['classification']['lifecycle'] == 'idle'


def test_pending_expiry_and_noon_cutoff():
    extra = [breakout()] + [bar(DAY, i, 101.3, 101.6, 101.25, 101.4) for i in range(16, 31)]
    assert snapshot(extra)['classification']['lifecycle'] == 'expired'
    extra = [bar(DAY, i) for i in range(15, 150)] + [bar(DAY, 150, 100, 102, 100, 101.5, 200)]
    assert snapshot(extra)['classification']['lifecycle'] == 'idle'


def test_forced_expiry_before_regular_and_early_close():
    extra = [breakout(), retest(), trigger()] + [bar(DAY, i, 101.6, 101.7, 101.4, 101.5) for i in range(18, 385)]
    assert snapshot(extra)['classification']['lifecycle'] == 'expired'
    early = date(2026, 11, 27)
    rec = dict(new_record(early), bars=[])
    for original in record(extra[:195])['bars']:
        row = dict(original)
        from core.session_bars import utc
        offset = utc(row['timestamp']) - session_time(DAY, 9, 30)
        row['timestamp'] = (session_time(early, 9, 30) + offset).isoformat()
        row['available_at'] = (session_time(early, 9, 30) + offset + timedelta(minutes=1)).isoformat()
        rec['bars'].append(row)
    rec['bars'] = rec['bars'][:205]
    prior = [bar(d, i) for d in preceding_sessions(early) for i in range(390)]
    assert build_snapshot(rec, prior, session_time(early, 12, 55))['classification']['lifecycle'] == 'expired'


def test_unfinished_breakout_cannot_trigger_and_late_backfill_cannot_originate():
    assert snapshot([breakout()], now=session_time(DAY, 9, 45, ) + timedelta(seconds=30))['classification']['lifecycle'] == 'idle'
    late = breakout()
    late['available_at'] = session_time(DAY, 9, 48).isoformat()
    assert snapshot([late, retest(), trigger()])['classification']['lifecycle'] == 'idle'


def test_late_continuation_cannot_trigger():
    late = trigger()
    late['available_at'] = session_time(DAY, 9, 50).isoformat()
    extra = [breakout(), retest(), late, bar(DAY, 18, 101.3, 101.4, 101.1, 101.2),
             bar(DAY, 19, 101.3, 101.4, 101.1, 101.2)]
    assert snapshot(extra)['classification']['phase'] == 'retest'


def test_event_identity_does_not_change_on_unchanged_minutes():
    original = snapshot([breakout(), retest(), trigger()])['classification']
    later = snapshot([breakout(), retest(), trigger(), bar(DAY, 18, 101.6, 101.7, 101.5, 101.6)])['classification']
    assert original['bar_at'] != later['bar_at']
    assert original['event_at'] == later['event_at']
    assert state_key(original) == state_key(later)


def expiry_record(day, end_minute):
    originals = record([breakout(), retest(), trigger()])['bars']
    rows = []
    for i in range(end_minute):
        if i < len(originals):
            original = originals[i]
            rows.append(bar(day, i, original['open'], original['high'], original['low'],
                            original['close'], original['volume']))
        else:
            rows.append(bar(day, i, 101.6, 101.7, 101.4, 101.5))
    prior = [bar(d, i) for d in preceding_sessions(day) for i in range(390)]
    return dict(new_record(day), bars=rows), prior


@pytest.mark.parametrize('day,deadline_minute', [(DAY, 385), (date(2026, 11, 27), 205)])
@pytest.mark.parametrize('missing_minutes', [1, 5])
def test_wall_clock_expiry_without_boundary_candle(day, deadline_minute, missing_minutes):
    from core.state_store import _snapshot
    from spy_setup.cog import SpySetupCog
    rec, prior = expiry_record(day, deadline_minute - missing_minutes)
    deadline = session_time(day, 9, 30) + timedelta(minutes=deadline_minute)
    result = build_snapshot(rec, prior, deadline + timedelta(seconds=5))
    c = result['classification']
    assert c['lifecycle'] == 'expired'
    assert c['event_at'] == deadline.isoformat()
    assert result['fresh'] == (missing_minutes == 1)
    assert c['available'] == result['fresh']
    assert result['events'][-1]['lifecycle'] == 'expired'
    assert 'volatile' not in json.dumps(result)
    _snapshot(result, day)  # Actual persistence contract, without changing it.
    assert SpySetupCog._decision_fresh(result, deadline + timedelta(seconds=5)) == result['fresh']
    if result['fresh']:
        _snapshot(result, day, observation=True)
    if missing_minutes > 1:
        assert c['direction'] == 'unavailable'
        assert 'Unavailable' in snapshot_embed(result).title
    again = build_snapshot(rec, prior, deadline + timedelta(seconds=10))
    assert again['classification']['event_at'] == c['event_at']


@pytest.mark.parametrize('day,deadline_minute', [(DAY, 385), (date(2026, 11, 27), 205)])
@pytest.mark.parametrize('offset,low,high,expected', [
    (0, 100.5, 104, 'invalidated'), (0, 101.4, 104, 'target_reached'),
    (0, 101.4, 101.7, 'expired'), (1, 100.5, 104, 'expired')])
def test_expiry_boundary_price_order(day, deadline_minute, offset, low, high, expected):
    rec, prior = expiry_record(day, deadline_minute + offset)
    rec['bars'][-1].update(low=low, high=high)
    end = session_time(day, 9, 30) + timedelta(minutes=deadline_minute + offset)
    result = build_snapshot(rec, prior, end + timedelta(seconds=5))
    assert result['classification']['lifecycle'] == expected
    assert result['classification']['event_at'] == (end - timedelta(minutes=offset)).isoformat()


def test_pending_wall_clock_expiry_with_stale_feed():
    result = snapshot([breakout()], now=session_time(DAY, 10, 1))
    assert result['classification']['lifecycle'] == 'expired'
    assert result['classification']['event_at'] == session_time(DAY, 10, 1).isoformat()
    assert not result['fresh'] and not result['classification']['available']
    assert 'volatile' not in json.dumps(result)


def reversal_bars():
    return [
        bar(DAY, 15, 100, 100, 98.5, 98.7, 200),  # put breakout
        bar(DAY, 16, 98.7, 100.3, 98.6, 100.2, 200),  # put invalidated only
        bar(DAY, 17, 100.2, 100.4, 99.4, 100.2, 200),  # later bullish reclaim
        bar(DAY, 18, 100.2, 100.4, 99.1, 100.2, 100),  # low-boundary retest
        bar(DAY, 19, 100.2, 102, 100, 100.8, 200),  # continuation, not target
    ]


def test_reversal_after_put_invalidation_requires_three_distinct_candles():
    rows = reversal_bars()
    first = snapshot(rows[:2])
    assert first['classification']['lifecycle'] == 'invalidated'
    assert len(first['events']) == 2
    reclaim = snapshot(rows[:3])
    c = reclaim['classification']
    assert c['kind'] == 'failed_breakdown_reversal'
    assert c['direction'] == 'bullish' and c['phase'] == 'breakout'
    assert c['trigger'] is None
    assert c['boundary'] == 99
    assert c['invalidation'] == pytest.approx(98.8)
    assert c['breakdown_at'] == session_time(DAY, 9, 46).isoformat()
    held = snapshot(rows[:4])['classification']
    assert held['phase'] == 'retest'
    assert held['trigger'] == pytest.approx(100.41)
    active = snapshot(rows)
    c = active['classification']
    assert c['lifecycle'] == 'triggered'
    assert c['entry_reference'] == 100.8
    assert c['invalidation'] == pytest.approx(99.09)
    assert c['target'] == pytest.approx(104.22)
    assert len(active['events']) == 5
    assert 'Failed-breakdown reversal' in snapshot_embed(active).title
    assert 'not an entry' in snapshot_embed(reclaim).description
    assert 'PDT' in snapshot_embed(active).description


@pytest.mark.parametrize('failure', ['no_break', 'wick_only', 'late_break', 'low_volume', 'below_vwap', 'late_reclaim'])
def test_reversal_requires_observed_break_and_confirmed_reclaim(failure):
    rows = reversal_bars()[:3]
    # Remove the bearish setup so failures isolate the reversal conditions.
    rows[0]['volume'] = 100
    if failure == 'no_break':
        rows[0] = bar(DAY, 15)
    elif failure == 'wick_only':
        rows[0]['close'] = 99.1
    elif failure == 'late_break':
        rows[0]['available_at'] = (session_time(DAY, 9, 47) + timedelta(seconds=31)).isoformat()
    elif failure == 'low_volume':
        rows[1]['volume'] = rows[2]['volume'] = 100
    elif failure == 'below_vwap':
        rows[1]['close'] = rows[2]['close'] = 99.2
    elif failure == 'late_reclaim':
        rows[1]['volume'] = 100
        rows[2]['available_at'] = (session_time(DAY, 9, 49) + timedelta(seconds=31)).isoformat()
    result = snapshot(rows, now=session_time(DAY, 9, 50))
    assert not any(e.get('kind') == 'failed_breakdown_reversal' for e in result['events'])


def test_reversal_shares_bullish_slot_and_invalidation_wins():
    rows = reversal_bars()
    rows += [bar(DAY, 20, 100.8, 106, 98.9, 100.2, 200),
             bar(DAY, 21), bar(DAY, 22, 100, 102, 100, 101.5, 200)]
    result = snapshot(rows)
    assert result['classification']['lifecycle'] == 'invalidated'
    assert len(result['events']) == 6
    assert sum(e['direction'] == 'bullish' and e['phase'] == 'breakout' for e in result['events']) == 1


def test_reversal_cannot_skip_retest_or_confirmation():
    rows = reversal_bars()
    rows[3] = bar(DAY, 18, 100.2, 102, 100, 101.5, 200)
    assert snapshot(rows[:4])['classification']['phase'] == 'breakout'
    rows = reversal_bars()
    rows[4]['volume'] = 100
    assert snapshot(rows)['classification']['phase'] == 'retest'
    rows[4]['volume'] = 200
    rows[4]['low'] = 98.5
    assert snapshot(rows)['classification']['lifecycle'] == 'invalidated'


def test_reversal_history_is_receipt_gated():
    result = snapshot(reversal_bars(), prior=history(receipt=session_time(DAY, 10)))
    assert result['reason'] == 'volume_baseline_unavailable'
    assert result['events'] == []


def test_reversal_deadline_is_reclaim_plus_fifteen_minutes():
    rows = reversal_bars()[:3]
    result = snapshot(rows)
    assert result['classification']['expires_at'] == session_time(DAY, 10, 3).isoformat()
    expired = snapshot(rows, now=session_time(DAY, 10, 3))
    assert expired['classification']['lifecycle'] == 'expired'
    assert expired['classification']['event_at'] == session_time(DAY, 10, 3).isoformat()
    assert not expired['classification']['available']


def test_embed_explicit_execution_boundary_and_stale_suppression():
    snap = snapshot([breakout(), retest(), trigger()])
    rendered = snapshot_embed(snap).to_dict()
    text = json.dumps(rendered)
    assert 'NOT SPX prices' in text and 'fresh SPX option quotes' in text
    assert VERSION in text and 'Unvalidated' in text
    snap['fresh'] = False
    rendered = snapshot_embed(snap).to_dict()
    assert 'Unavailable' in rendered['title']
    assert not any('SPY levels' in f['name'] for f in rendered['fields'])
