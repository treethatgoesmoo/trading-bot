"""Historical frozen ORB replay contracts; no retired collector/cog tests."""
import copy
from datetime import date, timedelta
import json
import unittest

from core.thetadata_client import IndexBar
from research.zero_dte.orb_audit import (
    evaluate_outcome, flow_filter, new_record, session_time, update_record,
)

DAY = date(2026, 9, 4)


def at(hour, minute=0, second=0):
    return session_time(DAY, hour, minute) + timedelta(seconds=second)


def bar(stamp, o=100, h=101, l=99, c=100):
    return IndexBar(stamp, o, h, l, c)


def opening():
    return [bar(at(9, 30) + timedelta(minutes=i)) for i in range(15)]


def breakout():
    return bar(at(9, 45), 100, 103, 100, 102)


def candidate(direction='UP'):
    return {'status': 'prospective', 'direction': direction, 'range_low': 99,
            'range_high': 101, 'available_at': at(9, 46, 10).isoformat(),
            'entry_time': at(9, 47).isoformat()}


def known(*bars):
    return {b.timestamp: {'open': b.open, 'high': b.high, 'low': b.low, 'close': b.close}
            for b in bars}


def flow(ratio=.2, **kwargs):
    return {'sample_at': at(9, 46).isoformat(), 'captured_at': at(9, 46, 5).isoformat(),
            'latest_trade_at': at(9, 45, 55).isoformat(),
            'total_trades': 100, 'classified_trades': 30, 'ratio': ratio, **kwargs}


class ORBEvaluatorTests(unittest.TestCase):
    def test_timing_entry_strictly_after_receipt_and_one_candidate(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(9, 46, 10), flow())
        first = copy.deepcopy(record['candidate'])
        self.assertEqual(first['status'], 'prospective')
        self.assertEqual(first['entry_time'], at(9, 47).isoformat())
        self.assertEqual(first['flow']['status'], 'qualifying')
        update_record(record, [bar(at(9, 46), 100, 100, 97, 98)], at(9, 47, 10))
        self.assertEqual(record['candidate'], first)
        exact = new_record(DAY)
        update_record(exact, opening() + [breakout()], at(9, 46))
        self.assertEqual(exact['candidate']['entry_time'], at(9, 47).isoformat())

    def test_late_startup_and_restart_cannot_relabel_first_break(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(10))
        self.assertEqual(record['candidate']['status'], 'missed')
        self.assertIsNone(record['candidate']['entry_time'])
        reloaded = json.loads(json.dumps(record))
        update_record(reloaded, [bar(at(10), 102, 104, 101, 103)], at(10, 1))
        self.assertEqual(reloaded['candidate'], record['candidate'])
        self.assertIsNone(reloaded['outcome'])

    def test_missing_opening_and_pre_signal_bars_never_filled(self):
        record = new_record(DAY)
        update_record(record, opening()[1:] + [breakout()], at(11))
        self.assertEqual(record['status'], 'missing_opening_range')
        self.assertIsNone(record['candidate'])
        record = new_record(DAY)
        update_record(record, opening() + [bar(at(9, 46), 102, 104, 101, 103)], at(9, 47))
        self.assertEqual(record['status'], 'missing_pre_signal_bar')
        self.assertIsNone(record['candidate'])

    def test_complete_valid_unique_bars_only_and_first_seen_immutable(self):
        record = new_record(DAY)
        bad = [bar(at(9, 44)), bar(at(9, 44)), bar(at(9, 45)),
               bar(at(9, 43, 1)), bar(at(9, 42), h=float('nan')),
               bar(at(9, 41), h=98), bar(at(9, 29))]
        update_record(record, [bar(at(9, 30))] + bad, at(9, 45))
        self.assertEqual(len(record['bars']), 1)
        first = copy.deepcopy(record['bars'][0])
        update_record(record, [bar(at(9, 30), h=200)], at(9, 46))
        self.assertEqual(record['bars'][0], first)
        self.assertGreater(record['rejected_bars'], 0)

    def test_request_cutoff_not_later_receipt_controls_completeness(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(9, 46, 1), data_cutoff=at(9, 45, 59))
        self.assertIsNone(record['candidate'])
        self.assertEqual(len(record['bars']), 15)
        update_record(record, [breakout()], at(9, 46, 20), data_cutoff=at(9, 46))
        self.assertEqual(record['candidate']['status'], 'prospective')
        self.assertEqual(record['candidate']['available_at'], at(9, 46, 20).isoformat())

    def test_fresh_poll_does_not_make_old_trades_fresh(self):
        result = flow_filter(flow(latest_trade_at=at(9, 42).isoformat()), at(9, 46, 10), 'UP')
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'stale_or_future_trade_data')
        self.assertEqual(flow_filter(flow(latest_trade_at=None), at(9, 46, 10), 'UP')['status'], 'missing')

    def test_flow_missing_filtered_qualifying_and_freshness(self):
        now = at(9, 46, 10)
        self.assertEqual(flow_filter(None, now, 'UP')['status'], 'missing')
        self.assertEqual(flow_filter(flow(0), now, 'UP')['status'], 'filtered')
        self.assertEqual(flow_filter(flow(-.2), now, 'DOWN')['status'], 'qualifying')
        for f in [flow(sample_at=at(9, 43).isoformat()), flow(sample_at=at(9, 47).isoformat()),
                  flow(captured_at=at(9, 47).isoformat()), flow(classified_trades=24),
                  flow(total_trades=200), flow(float('nan'))]:
            self.assertEqual(flow_filter(f, now, 'UP')['status'], 'missing')
        self.assertEqual(flow_filter(flow(sample_at=at(9, 44, 10).isoformat()), now, 'UP')['status'], 'qualifying')

    def test_ambiguous_bar_stop_first(self):
        result = evaluate_outcome(candidate(), known(bar(at(9, 47), 102, 106, 98, 103)), at(9, 48))
        self.assertEqual(result['r_multiple'], -1)
        self.assertTrue(result['ambiguous_stop_target'])
        self.assertTrue(result['not_option_pnl'])
        self.assertEqual(result['mfe_points_upper_bound'], 4)
        self.assertEqual(result['mae_points_upper_bound'], 4)

    def test_gap_stop_adverse_and_target_capped(self):
        first = bar(at(9, 47), 102, 103, 101, 102)
        result = evaluate_outcome(candidate(), known(first, bar(at(9, 48), 97, 100, 96, 98)), at(9, 49))
        self.assertEqual(result['reason'], 'gap_stop')
        self.assertLess(result['r_multiple'], -1)
        result = evaluate_outcome(candidate(), known(first, bar(at(9, 48), 107, 108, 98, 104)), at(9, 49))
        self.assertEqual(result['reason'], 'gap_target_capped')
        self.assertEqual(result['r_multiple'], 1)
        self.assertFalse(result['ambiguous_stop_target'])

    def test_short_target_and_invalid_gap_entry(self):
        result = evaluate_outcome(candidate('DOWN'), known(bar(at(9, 47), 98, 99, 94, 96)), at(9, 48))
        self.assertEqual(result['r_multiple'], 1)
        result = evaluate_outcome(candidate(), known(bar(at(9, 47), 98, 100, 97, 99)), at(9, 48))
        self.assertEqual(result['status'], 'invalid_entry')

    def test_horizon_and_gap_missing_not_forward_filled(self):
        bars = [bar(at(9, 47) + timedelta(minutes=i), 102, 103, 101, 102.5) for i in range(30)]
        self.assertIsNone(evaluate_outcome(candidate(), known(*bars), at(10, 16, 59)))
        result = evaluate_outcome(candidate(), known(*bars), at(10, 17))
        self.assertEqual(result['reason'], 'time_exit')
        self.assertEqual(result['signed_points'], .5)
        del bars[5]
        self.assertIsNone(evaluate_outcome(candidate(), known(*bars), at(10, 17)))
        self.assertEqual(evaluate_outcome(candidate(), known(*bars), at(10, 18, 30))['reason'], 'missing_outcome_bar')
        self.assertEqual(evaluate_outcome(candidate(), {}, at(10, 19))['reason'], 'missing_entry_bar')

    def test_no_breakout_cutoff_and_no_backdated_entry(self):
        record = new_record(DAY)
        bars = opening() + [bar(at(9, 45) + timedelta(minutes=i)) for i in range(74)]
        update_record(record, bars + [bar(at(10, 59), 100, 104, 99, 103)], at(11))
        self.assertEqual(record['status'], 'no_breakout')
        self.assertIsNone(record['candidate'])
        c = candidate()
        c['entry_time'] = c['available_at']
        with self.assertRaises(ValueError):
            evaluate_outcome(c, {}, at(10))


if __name__ == '__main__':
    unittest.main()
