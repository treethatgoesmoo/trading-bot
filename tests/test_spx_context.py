"""Synthetic as-of contracts, not strategy profitability validation."""
from datetime import date, timedelta
import json
import unittest

from core import market_calendar
from zero_dte.orb_audit import new_record, session_time
from zero_dte.spx_context import build_context, grade_context
from zero_dte.spx_context_embeds import context_embed

DAY = date(2026, 9, 8)  # Prior trading session is Friday before Labor Day.


def at(h, m=0, s=0, day=DAY):
    return session_time(day, h, m) + timedelta(seconds=s)


def bar(stamp, o=100, h=101, l=99, c=100, receipt=None):
    return dict(timestamp=stamp.isoformat(), open=o, high=h, low=l, close=c,
                available_at=(receipt or stamp + timedelta(minutes=1)).isoformat())


def record(extra=()):
    r = new_record(DAY)
    r['bars'] = [bar(at(9, 30) + timedelta(minutes=i)) for i in range(15)] + list(extra)
    return r


def flow(ratio=.4, now=None):
    now = now or at(9, 46, 10)
    return dict(sample_at=(now-timedelta(seconds=10)).isoformat(), captured_at=now.isoformat(),
                latest_trade_at=(now-timedelta(seconds=15)).isoformat(),
                total_trades=100, classified_trades=50, ratio=ratio, new_trade_rows=10)


class ContextTests(unittest.TestCase):
    def test_opening_map_and_json_contract(self):
        result = build_context(record(), [], at(9, 45, 10))
        self.assertTrue(result['fresh'])
        self.assertEqual(result['direction'], 'NEUTRAL')
        self.assertEqual(result['setup']['state'], 'inside_range')
        self.assertIn('99.00–101.00', result['structure']['text'])
        self.assertEqual(result['version'], 'spx-context-v1')
        self.assertEqual(set(result['sources']), {'spx','prior_spx','spy_proxy','options_flow'})
        json.dumps(result, allow_nan=False)

    def test_confirmation_reversals_change_signature_even_inside_range(self):
        r = record([bar(at(9, 45))])
        now = at(9, 46, 10)
        up = build_context(r, [], now, flow(.4))
        down = build_context(r, [], now, flow(-.4))
        self.assertEqual(up['direction'], 'NEUTRAL')
        self.assertEqual(down['direction'], 'NEUTRAL')
        self.assertNotEqual(up['signature'], down['signature'])
        proxy = dict(status='available', above_vwap=True, volume_ratio=1.0, bar_at=at(9, 45).isoformat())
        above = build_context(r, [], now, proxy=proxy)
        below = build_context(r, [], now, proxy=dict(proxy, above_vwap=False))
        self.assertNotEqual(above['signature'], below['signature'])
        from unittest.mock import MagicMock
        from zero_dte.spx_context_cog import SPXContextCog
        cog = SPXContextCog(MagicMock(), MagicMock())
        state = dict(session_date=str(DAY), last_published_at=at(9, 44).isoformat(),
                     last_published_signature=up['signature'])
        from unittest.mock import patch
        with patch('config.SPX_CONTEXT_CHANNEL_ID', 1):
            self.assertEqual(cog._reason(state, down, now), 'state_change')

    def test_break_hold_retest_failure_and_reclaim(self):
        r = record([bar(at(9,45),100,103,100,102)])
        first = build_context(r, [], at(9,46,10))
        self.assertEqual(first['setup']['state'], 'breakout_up')
        self.assertEqual(first['direction'], 'UP')
        self.assertIn('at/below 101.00', first['invalidation']['text'])
        r['bars'].append(bar(at(9,46),102,104,102,103))
        self.assertEqual(build_context(r, [], at(9,47,10))['setup']['state'], 'holding_up')
        r['bars'].append(bar(at(9,47),103,104,101,102))
        self.assertEqual(build_context(r, [], at(9,48,10))['setup']['state'], 'retest_up')
        r['bars'].append(bar(at(9,48),102,102,99,100))
        failure = build_context(r, [], at(9,49,10))
        self.assertEqual(failure['setup']['state'], 'failed_up')
        self.assertEqual(failure['direction'], 'NEUTRAL')
        r['bars'].append(bar(at(9,49),100,103,100,102))
        self.assertEqual(build_context(r, [], at(9,50,10))['setup']['state'], 'reclaimed_up')

    def test_downside_and_exact_boundary_are_symmetric(self):
        r = record([bar(at(9,45),100,100,97,98)])
        result = build_context(r, [], at(9,46,10))
        self.assertEqual(result['setup']['state'], 'breakout_down')
        self.assertEqual(result['direction'], 'DOWN')
        self.assertIn('at/above 99.00', result['invalidation']['text'])
        r['bars'].append(bar(at(9,46),98,99,97,99))
        result = build_context(r, [], at(9,47,10))
        self.assertEqual(result['setup']['state'], 'failed_down')
        self.assertEqual(result['direction'], 'NEUTRAL')

    def test_partial_future_and_not_yet_received_bars_excluded(self):
        r = record([bar(at(9,45),100,103,100,102)])
        result = build_context(r, [], at(9,45,30))
        self.assertEqual(result['setup']['state'], 'inside_range')
        r['bars'][-1]['available_at'] = at(9,47).isoformat()
        self.assertNotEqual(build_context(r, [], at(9,46,10))['setup']['state'], 'breakout_up')

    def test_staleness_gaps_and_duplicates_do_not_become_setups(self):
        r = record([bar(at(9,45),100,103,100,102)])
        result = build_context(r, [], at(10))
        self.assertFalse(result['fresh'])
        self.assertEqual(result['direction'], 'NEUTRAL')
        self.assertEqual(result['setup']['state'], 'indeterminate')
        for changed in ('gap', 'duplicate'):
            r = record([bar(at(9,45),100,103,100,102)])
            if changed == 'gap':
                r['bars'].pop(4)
            else:
                r['bars'].append(dict(r['bars'][4]))
            result = build_context(r, [], at(9,46,10))
            self.assertEqual(result['setup']['state'], 'indeterminate')
            self.assertEqual(result['direction'], 'NEUTRAL')

    def test_forming_and_after_session_never_directional(self):
        r = record()
        self.assertEqual(build_context(r, [], at(9,40))['structure']['state'], 'forming')
        self.assertFalse(build_context(r, [], at(16,1))['fresh'])
        self.assertFalse(build_context(r, [], at(9,45,day=date(2026,9,9)))['fresh'])

    def test_previous_session_requires_complete_immediate_trading_day(self):
        previous = date(2026,9,4)
        bars = [bar(at(9,30,day=previous)+timedelta(minutes=i)) for i in range(390)]
        result = build_context(record(), bars, at(9,45,10))
        self.assertTrue(result['sources']['prior_spx']['used'])
        self.assertIn('Prior-session high/low/close', result['structure']['text'])
        self.assertFalse(build_context(record(), bars[1:], at(9,45,10))['sources']['prior_spx']['used'])
        old = [bar(at(9,30,day=date(2026,9,3))+timedelta(minutes=i)) for i in range(390)]
        self.assertFalse(build_context(record(), old, at(9,45,10))['sources']['prior_spx']['used'])

    def test_previous_early_close_requires_210_minutes_not_390(self):
        day = date(2026,11,30)
        previous = date(2026,11,27)
        r = new_record(day)
        r['bars'] = [bar(at(9,30,day=day)+timedelta(minutes=i)) for i in range(15)]
        bars = [bar(at(9,30,day=previous)+timedelta(minutes=i)) for i in range(210)]
        self.assertTrue(build_context(r,bars,at(9,45,10,day=day))['sources']['prior_spx']['used'])

    def test_momentum_uses_completed_nonoverlapping_five_minutes(self):
        r = record()
        r['bars'] = [bar(at(9,30)+timedelta(minutes=i),100,101,99,100) for i in range(5)]
        r['bars'] += [bar(at(9,35)+timedelta(minutes=i),100+i,102+i,100+i,101+i) for i in range(5)]
        r['bars'] += [bar(at(9,40)+timedelta(minutes=i),105+2*i,108+2*i,105+2*i,107+2*i) for i in range(5)]
        first = build_context(r, [], at(9,45,10))
        self.assertEqual(first['momentum']['state'], 'up_strengthening')
        # One completed minute in the next 5m candle must not alter the comparison.
        r['bars'].append(bar(at(9,45),115,120,90,92))
        second = build_context(r, [], at(9,46,10))
        self.assertEqual(first['momentum'], second['momentum'])
        r['bars'].pop(8)
        self.assertEqual(build_context(r, [], at(9,46,10))['momentum']['state'], 'unavailable')

    def test_options_flow_and_proxy_do_not_change_price_direction(self):
        r = record([bar(at(9,45),100,103,100,102)])
        proxy = dict(status='available', above_vwap=False, volume_ratio=.8,
                     bar_at=at(9,45).isoformat(), source='yfinance')
        result = build_context(r, [], at(9,46,10), flow(-.4), proxy)
        self.assertEqual(result['direction'], 'UP')
        self.assertEqual(result['sources']['options_flow'], {'used':True,'status':'opposed'})
        self.assertEqual(result['sources']['spy_proxy'], {'used':True,'status':'opposed'})
        self.assertIn('SPY volume is not SPX volume', result['confirmation']['text'])
        stale = flow()
        stale['latest_trade_at'] = at(9,40).isoformat()
        result = build_context(r, [], at(9,46,10), stale)
        self.assertFalse(result['sources']['options_flow']['used'])
        self.assertEqual(result['direction'], 'UP')

    def test_proxy_stale_future_nonfinite_or_missing_not_confirmation(self):
        for stamp, ratio in [(at(9,40),1), (at(9,47),1),(at(9,45),float('nan'))]:
            proxy = dict(status='available',above_vwap=True,volume_ratio=ratio,bar_at=stamp.isoformat())
            self.assertFalse(build_context(record(),[],at(9,46,10),proxy=proxy)['sources']['spy_proxy']['used'])

    def test_signature_excludes_numeric_level_drift(self):
        r = record([bar(at(9,45),100,103,100,102),bar(at(9,46),102,104,102,103)])
        first = build_context(r,[],at(9,47,10))
        r['bars'].append(bar(at(9,47),103,105,103,104))
        second = build_context(r,[],at(9,48,10))
        self.assertEqual(first['signature'],second['signature'])
        self.assertNotEqual(first['structure']['text'],second['structure']['text'])

    def test_embed_exact_fields_limits_and_no_probability_claim(self):
        result = build_context(record(), [], at(9,45,10))
        embed = context_embed(result)
        self.assertEqual([f.name for f in embed.fields], ['Action','Structure','Setup','Momentum','Confirmation','Invalidation'])
        self.assertLess(len(embed),6000)
        self.assertIn('Experimental action checklist',embed.description)
        self.assertIn('no contract selection or proven edge',embed.description)
        self.assertIn('not option P&L',embed.footer.text)


class GradingTests(unittest.TestCase):
    def assessment(self,direction='UP',when=None):
        return dict(session_date=DAY.isoformat(),as_of=(when or at(9,46,10)).isoformat(),direction=direction,fresh=True)

    def held(self):
        return [bar(at(9,47)+timedelta(minutes=i),100,103,99,102) for i in range(30)]

    def test_next_minute_open_and_fifteen_thirty_minute_horizons(self):
        result = grade_context(self.assessment(),self.held(),at(10,17))
        self.assertEqual(result['15']['entry_time'],at(9,47).isoformat())
        self.assertEqual(result['15']['exit_time'],at(10,2).isoformat())
        self.assertEqual(result['30']['exit_time'],at(10,17).isoformat())
        self.assertEqual(result['15']['points'],2)
        self.assertEqual(result['15']['signed_points'],2)
        self.assertEqual(grade_context(self.assessment('DOWN'),self.held(),at(10,17))['30']['signed_points'],-2)
        self.assertIsNone(grade_context(self.assessment('NEUTRAL'),self.held(),at(10,17))['30']['signed_points'])

    def test_exact_minute_decision_cannot_use_same_open(self):
        result = grade_context(self.assessment(when=at(9,46)),self.held(),at(10,17))
        self.assertEqual(result['15']['entry_time'],at(9,47).isoformat())

    def test_pending_missing_and_late_receipts(self):
        self.assertIsNone(grade_context(self.assessment(),self.held(),at(10,1,59))['15'])
        bars = self.held()[1:]
        self.assertIsNone(grade_context(self.assessment(),bars,at(10,3))['15'])
        self.assertEqual(grade_context(self.assessment(),bars,at(10,4))['15']['status'],'missing')
        bars = self.held()
        bars[-1]['available_at'] = at(10,20).isoformat()
        self.assertIsNone(grade_context(self.assessment(),bars,at(10,17))['30'])
        self.assertEqual(grade_context(self.assessment(),bars,at(10,19))['30']['status'],'missing')

    def test_missing_intermediate_minute_not_only_entry_and_exit(self):
        bars = self.held()
        bars.pop(5)
        result = grade_context(self.assessment(),bars,at(10,20))
        self.assertEqual(result['15']['status'],'missing')
        self.assertEqual(result['30']['status'],'missing')

    def test_invalid_stale_context_and_close_horizons(self):
        assessment = self.assessment()
        assessment['fresh'] = False
        self.assertEqual(grade_context(assessment,[],at(10))['15']['status'],'invalid_entry')
        assessment = self.assessment(when=at(15,40))
        result = grade_context(assessment,[],at(15,40))
        self.assertIsNone(result['15'])
        self.assertEqual(result['30']['status'],'session_ended')
        day = date(2026,11,27)
        assessment.update(session_date=day.isoformat(),as_of=at(12,40,day=day).isoformat())
        result = grade_context(assessment,[],at(12,40,day=day))
        self.assertIsNone(result['15'])
        self.assertEqual(result['30']['status'],'session_ended')


if __name__ == '__main__':
    unittest.main()
