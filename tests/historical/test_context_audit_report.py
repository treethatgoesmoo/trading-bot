"""Synthetic, read-only context reporting: delivery, lineage and as-of evidence."""
from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest

from research.core.context_audit_report import context_report
from research.zero_dte.orb_audit import new_record, session_time
from research.zero_dte.spx_context import grade_context

DAY = date(2026, 9, 4)
END = DAY + timedelta(days=1)


def at(h, m=0, s=0):
    return session_time(DAY, h, m) + timedelta(seconds=s)


def bars():
    return [dict(timestamp=(at(9, 30) + timedelta(minutes=i)).isoformat(),
                 available_at=(at(9, 31) + timedelta(minutes=i)).isoformat(),
                 open=100, high=102, low=99, close=101) for i in range(91)]


def entry(identity='alert-a', delivery='sent', direction='UP', flow=False):
    sources = {s: dict(used=False, status='unavailable')
               for s in ('spx', 'prior_spx', 'spy_proxy', 'options_flow')}
    sources['spx'] = dict(used=True, status='fresh')
    if flow:
        sources['options_flow'] = dict(used=True, status='aligned')
    assessment = dict(schema=1, version='spx-context-v1', session_date=str(DAY),
                      as_of=at(9, 46, 10).isoformat(), bar_at=at(9, 45).isoformat(),
                      fresh=True, direction=direction, sources=sources)
    result = dict(id=identity, assessment=assessment, evaluated_at=assessment['as_of'],
                  delivery=delivery, delivered_at=at(9, 46, 11).isoformat() if delivery == 'sent' else None,
                  suppression_reason='first_read',
                  request_ids=dict(spx='index-a', prior_spx=None, options_flow='flow-a' if flow else None),
                  consumed_request_ids=dict(spx=['index-a'], prior_spx=[], options_flow=['flow-a'] if flow else []),
                  observed_request_ids=dict(spx='unused-acquisition'), outcomes={'15': None, '30': None})
    if delivery == 'sent':
        result['outcomes'] = grade_context(dict(assessment, as_of=result['delivered_at']), bars(), at(11))
    return result


def state(*entries):
    record = new_record(DAY)
    record['bars'] = bars()
    return dict(schema=1, version='spx-context-v1', session_date=str(DAY), record=record,
                assessments=list(entries), polls=len(entries), last_poll_at=at(11).isoformat())


def usage(identity='index-a', product='index', purpose='audit', success=True, finish=None):
    begin = dict(schema=1, event='start', request_id=identity, product=product, purpose=purpose,
                 endpoint='index_history_ohlc' if product == 'index' else 'option_history_trade_quote',
                 started_at=at(9, 45).isoformat())
    end = dict(begin, event='finish', finished_at=(finish or at(9, 46)).isoformat(),
               elapsed_ms=60000, success=success, rows=20, error_class=None)
    return begin, end


class ContextReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / f'context-{DAY}.json'

    def save(self, value, saved=None):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        stamp = (saved or at(11)).timestamp()
        os.utime(self.path, (stamp, stamp))

    def report(self, cutoff=None, pairs=None):
        return '\n'.join(context_report(self.directory, DAY, END, cutoff or at(12), usage_pairs=pairs))

    def test_action_report_separates_candidates_waits_legacy_and_invalid(self):
        from test_spx_actions import candidate
        from research.zero_dte.spx_actions import build_action
        def with_action(identity, delivery='sent'):
            item = entry(identity, delivery)
            ctx = candidate()
            for key in ('structure', 'setup', 'momentum', 'invalidation'):
                item['assessment'][key] = ctx[key]
            item['assessment']['action'] = build_action(item['assessment'])
            return item
        call = with_action('call')
        wait = with_action('wait')
        wait['assessment']['setup']['state'] = 'holding_up'
        wait['assessment']['action'] = build_action(wait['assessment'])
        invalid = with_action('invalid')
        invalid['assessment']['action']['candidate_direction'] = 'DOWN'
        pending = with_action('pending')
        pending['outcomes'] = {'15': None, '30': None}
        self.save(state(call, wait, entry('legacy'), invalid, pending, with_action('suppressed', 'suppressed')))
        before = self.path.read_bytes()
        text = self.report(pairs=[]).split('EXPERIMENTAL ACTIONS / SENT ONLY')[1]
        self.assertIn('Recorded valid actions=3 unclassified=2 invalid_or_unknown_version=1', text)
        self.assertIn('CONSIDER CALLS — experimental: sent=2', text)
        self.assertIn('15m: complete=1 pending=1', text)
        self.assertIn('directional_n=1 mean_signed_SPX_points=+1.000', text)
        self.assertIn("WAIT FOR RETEST — DON'T CHASE: sent=1", text)
        self.assertIn('Non-entry action: no directional action score', text)
        self.assertIn('no action-input credit', text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_put_actions_keep_missing_and_stale_delivery_out_of_scores(self):
        from test_spx_actions import candidate
        from research.zero_dte.spx_actions import build_action
        rows = []
        for identity in ('put', 'missing', 'late'):
            item = entry(identity, direction='DOWN')
            ctx = candidate('DOWN')
            for key in ('structure', 'setup', 'momentum', 'invalidation'):
                item['assessment'][key] = ctx[key]
            item['assessment']['action'] = build_action(item['assessment'])
            rows.append(item)
        rows[1]['outcomes']['15'] = dict(status='missing', reason='missing_held_minutes')
        rows[2]['delivered_at'] = at(10).isoformat()  # purported completion must fail causal validation
        self.save(state(*rows))
        text = self.report(pairs=[]).split('EXPERIMENTAL ACTIONS / SENT ONLY')[1]
        self.assertIn('CONSIDER PUTS — experimental: sent=3', text)
        self.assertIn('15m: complete=1 pending=0 missing=1 invalid_entry=0 session_ended=0 unknown=1', text)
        self.assertIn('directional_n=1 mean_signed_SPX_points=-1.000', text)
        # A retrospective cutoff must not reveal later delivery or later outcomes.
        early = self.report(at(9, 50), []).split('EXPERIMENTAL ACTIONS / SENT ONLY')[1]
        self.assertIn('CONSIDER PUTS — experimental: sent=2', early)
        self.assertIn('directional_n=0 mean_signed_SPX_points=n/a', early)

    def test_empty_directory_is_not_zero_usage(self):
        text = self.report()
        self.assertIn('expected=1 present=0 valid=0 missing=1 corrupt=0', text)
        self.assertIn('NO CONTEXT EVIDENCE', text)
        self.assertIn('Evaluated assessments=0', text)
        self.assertIn('No matching telemetry denominator', text)

    def test_sent_and_all_consumption_are_separate_and_cached_ids_are_unioned(self):
        a, b, c = entry(flow=True), entry('alert-b', 'suppressed', flow=True), entry('alert-c', 'sent')
        c['request_ids']['spx'] = 'index-b'
        c['consumed_request_ids']['spx'] = ['index-a', 'index-b']
        self.save(state(a, b, c))
        text = self.report(pairs=[usage(), usage('index-b'), usage('flow-a', 'options'), usage('unused-acquisition')])
        self.assertIn('Evaluated assessments=3 sent=2 suppressed=1 failed=0 unknown=0 pending=0', text)
        self.assertIn('options_flow: used=2/3 unused=1', text)
        self.assertIn('options_flow: used=1/2 unused=1', text)
        self.assertIn('referenced=3 matched_success=3 unmatched=0', text)
        self.assertIn('index/audit: consumed_success=2 / observed_success=3; observed_completions=3', text)
        self.assertIn('15m: sent=2 complete=2 pending=0', text)
        self.assertIn('directional_n=2 mean_signed_SPX_points=+1.000', text)

    def test_explicit_constituents_exclude_latest_empty_refresh(self):
        item = entry(flow=True)
        item['request_ids']['options_flow'] = 'flow-empty'
        self.save(state(item))
        empty = usage('flow-empty', 'options', purpose='spx_context_flow')
        empty[1]['rows'] = 0
        text = self.report(pairs=[usage(), usage('flow-a', 'options', purpose='spx_context_flow'), empty])
        self.assertIn('options/spx_context_flow: consumed_success=1 / observed_success=2', text)
        item['consumed_request_ids']['options_flow'] = []
        self.save(state(item))
        text = self.report(pairs=[usage(), empty])
        self.assertIn('options/spx_context_flow: consumed_success=0 / observed_success=1', text)

    def test_all_delivery_statuses_are_visible_and_only_sent_is_graded(self):
        self.save(state(*(entry('alert-' + d, d) for d in ('sent', 'suppressed', 'failed', 'unknown', 'pending'))))
        text = self.report(pairs=[])
        self.assertIn('sent=1 suppressed=1 failed=1 unknown=1 pending=1', text)
        self.assertIn('15m: sent=1 complete=1', text)

    def test_neutral_is_not_scored_and_downside_signed_points_are_checked(self):
        self.save(state(entry('neutral', direction='NEUTRAL'), entry('down', direction='DOWN')))
        text = self.report(pairs=[])
        self.assertIn('complete_UP=0 complete_DOWN=1 neutral_complete=1 directional_n=1 mean_signed_SPX_points=-1.000', text)

    def test_missing_finalized_outcome_never_improves_with_backfilled_bars(self):
        item = entry()
        item['outcomes']['15'] = dict(status='missing', reason='missing_held_minutes')
        self.save(state(item))
        before = self.path.read_bytes()
        text = self.report(pairs=[])
        self.assertIn('15m: sent=1 complete=0 pending=0 missing=1', text)
        self.assertEqual(self.path.read_bytes(), before)

    def test_pending_is_not_upgraded_even_with_full_evidence(self):
        item = entry()
        item['outcomes'] = {'15': None, '30': None}
        self.save(state(item))
        self.assertIn('15m: sent=1 complete=0 pending=1', self.report(pairs=[]))

    def test_historical_cutoff_withholds_future_delivery_and_mutable_statuses(self):
        sent = entry()
        sent['delivered_at'] = at(10).isoformat()
        self.save(state(sent, entry('suppressed', 'suppressed'), entry('failed', 'failed')))
        text = self.report(at(9, 50), [])
        self.assertIn('Evaluated assessments=3 sent=0 suppressed=0 failed=0 unknown=2 pending=1', text)
        self.assertIn('future_snapshots=1', text)

    def test_historical_terminal_outcome_unknown_but_complete_proven_from_bars(self):
        item = entry()
        item['outcomes']['30'] = dict(status='missing', reason='missing_held_minutes')
        self.save(state(item))
        text = self.report(at(10, 20), [])
        self.assertIn('15m: sent=1 complete=1 pending=0', text)
        self.assertIn('30m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=0 unknown=1', text)

    def test_complete_future_horizon_pending_and_future_received_bars_unknown(self):
        self.save(state(entry()))
        text = self.report(at(10), [])
        self.assertIn('15m: sent=1 complete=0 pending=1', text)
        value = state(entry())
        value['record']['bars'][17]['available_at'] = at(11).isoformat()  # entry not received by cutoff
        self.save(value)
        self.assertIn('15m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=0 unknown=1',
                      self.report(at(10, 20), []))

    def test_file_mtime_prevents_false_asof_finalization_from_early_heartbeat(self):
        item = entry()
        item['outcomes']['15'] = dict(status='missing')
        value = state(item)
        value['last_poll_at'] = at(9, 46).isoformat()
        self.save(value, saved=at(11))
        text = self.report(at(10, 20), [])
        self.assertIn('15m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=0 unknown=1', text)

    def test_asof_known_invalid_and_session_ended_statuses_remain_visible(self):
        item = entry()
        item['outcomes'] = {'15': dict(status='invalid_entry', reason='delivery_stale'),
                            '30': dict(status='session_ended', reason='horizon_crosses_close')}
        self.save(state(item))
        text = self.report(pairs=[])
        self.assertIn('15m: sent=1 complete=0 pending=0 missing=0 invalid_entry=1', text)
        self.assertIn('30m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=1', text)

    def test_duplicate_ids_count_once_conflicts_exclude_both(self):
        a, bad = entry(), entry()
        bad['assessment']['direction'] = 'DOWN'
        self.save(state(a, deepcopy(a), bad, entry('good')))
        text = self.report(pairs=[])
        self.assertIn('Evaluated assessments=1 sent=1', text)
        self.assertIn('duplicates=1 conflicts=1', text)

    def test_bad_entries_and_nonfinite_values_quarantined_without_printing_content(self):
        a, b, c = entry('bad-a'), entry('bad-b'), entry('bad-c')
        a['assessment']['direction'] = 'SECRET_TOKEN'
        b['assessment']['sources']['spx']['used'] = 'yes'
        c['outcomes']['15']['points'] = float('nan')
        self.save(state(a, b, c, entry('good')))
        text = self.report(pairs=[])
        self.assertIn('Evaluated assessments=1 sent=1', text)
        self.assertIn('invalid_entries=3', text)
        self.assertNotIn('SECRET_TOKEN', text)

    def test_corrupt_file_bad_schema_and_partition_quarantined(self):
        self.path.write_text('{bad', encoding='utf-8')
        self.assertIn('valid=0 missing=0 corrupt=1', self.report())
        for field, bad in (('schema', 2), ('schema', True), ('session_date', '2026-09-03'), ('record', {})):
            value = state(entry())
            value[field] = bad
            self.save(value)
            self.assertIn('valid=0 missing=0 corrupt=1', self.report())

    def test_wrong_points_noncausal_entry_and_missing_held_bar_excluded(self):
        for mutation in ('points', 'entry_time', 'gap'):
            value = state(entry())
            if mutation == 'points':
                value['assessments'][0]['outcomes']['15']['points'] = 100
            elif mutation == 'entry_time':
                value['assessments'][0]['outcomes']['15']['entry_time'] = at(9, 46).isoformat()
            else:
                del value['record']['bars'][20]
            self.save(value)
            text = self.report(pairs=[])
            self.assertIn('15m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=0 unknown=1', text)
            self.assertNotIn('invalid_outcomes=0', text)

    def test_completed_outcome_cannot_use_stale_discord_delivery(self):
        item = entry()
        item['delivered_at'] = at(9, 48).isoformat()  # bar ended 09:46; too late
        item['outcomes'] = grade_context(dict(item['assessment'], as_of=item['delivered_at']), bars(), at(11))
        self.save(state(item))
        text = self.report(pairs=[])
        self.assertIn('invalid_outcomes=2', text)
        self.assertIn('15m: sent=1 complete=0 pending=0 missing=0 invalid_entry=0 session_ended=0 unknown=1', text)

    def test_bar_integrity_diagnostics_and_duplicate_exclusion(self):
        value = state(entry())
        value['record']['bars'].append(deepcopy(value['record']['bars'][20]))
        value['record']['bars'].append(dict(timestamp='bad', available_at='bad'))
        self.save(value)
        text = self.report(pairs=[])
        self.assertIn('invalid_bars=1 duplicate_bars=1', text)
        self.assertIn('invalid_outcomes=2', text)

    def test_freshness_claim_without_current_bar_is_invalid(self):
        for stamp in (None, at(9, 40).isoformat(), at(9, 45, 1).isoformat()):
            item = entry()
            item['assessment']['bar_at'] = stamp
            self.save(state(item))
            self.assertIn('invalid_entries=1', self.report(pairs=[]))

    def test_neutral_signed_score_is_invalid(self):
        item = entry(direction='NEUTRAL')
        item['outcomes']['15']['signed_points'] = 1
        self.save(state(item))
        self.assertIn('invalid_outcomes=1', self.report(pairs=[]))

    def test_source_missing_stale_and_proxy_not_theta_requests(self):
        item = entry()
        item['assessment']['fresh'] = False
        item['assessment']['sources']['spx'] = dict(used=False, status='stale_or_missing')
        item['assessment']['sources']['options_flow'] = dict(used=False, status='stale_or_future_trade_data')
        item['assessment']['sources']['spy_proxy'] = dict(used=True, status='aligned')
        item['request_ids']['options_flow'] = 'flow-a'  # invalid stale reference gets no credit
        item['consumed_request_ids']['options_flow'] = ['flow-a']
        self.save(state(item))
        text = self.report(pairs=[usage(), usage('flow-a', 'options')])
        self.assertIn('spx: used=0/1 unused=1; stale_or_missing=1', text)
        self.assertIn('spy_proxy: used=1/1 unused=0', text)
        self.assertIn('referenced=0 matched_success=0', text)
        self.assertIn('ignored_refs_on_unused_sources=2', text)

    def test_unmatched_failed_wrong_product_and_noncausal_refs_are_not_usage_credit(self):
        item = entry(flow=True)
        item['consumed_request_ids']['spx'] += ['absent', 'failure', 'future', 'wrong-product']
        self.save(state(item))
        text = self.report(pairs=[usage(), usage('failure', success=False),
                                 usage('future', finish=at(10)), usage('wrong-product', 'options')])
        self.assertIn('referenced=6 matched_success=1 unmatched=2 failed_completion_refs=1 product_mismatch=1 noncausal_reference_occurrences=1', text)

    def test_missing_ids_visible_and_legacy_representative_fallback(self):
        item = entry()
        item.pop('consumed_request_ids')
        self.save(state(item))
        self.assertIn('referenced=1 matched_success=1', self.report(pairs=[usage()]))
        item['request_ids']['spx'] = None
        self.save(state(item))
        self.assertIn('used_source_assessments_without_ids=1', self.report(pairs=[]))

    def test_direct_telemetry_read_deduplicates_and_quarantines_conflicts(self):
        self.save(state(entry()))
        begin, finish = usage()
        events = [begin, finish, deepcopy(finish), *usage('unused')]
        path = self.directory / f'usage-{DAY}.jsonl'
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\ninvalid\n', encoding='utf-8')
        text = self.report()
        self.assertIn('consumed_success=1 / observed_success=2; observed_completions=2', text)
        self.assertIn('usage_invalid=1 usage_duplicates=1 usage_conflicts=0', text)
        finish['success'] = False
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(finish) + '\n')
        text = self.report()
        self.assertIn('referenced=1 matched_success=0 unmatched=1', text)
        self.assertIn('usage_conflicts=1', text)

    def test_orphan_finish_is_observed_completion_but_start_only_is_not(self):
        self.save(state(entry()))
        begin, finish = usage()
        text = self.report(pairs=[(None, finish), (usage('started')[0], None)])
        self.assertIn('consumed_success=1 / observed_success=1; observed_completions=1', text)
        self.assertIn('matched_success=1', text)

    def test_telemetry_future_and_out_of_window_ignored(self):
        self.save(state(entry()))
        old = list(usage('old'))
        for row in old:
            row['started_at'] = (at(9, 45) - timedelta(days=1)).isoformat()
        text = self.report(pairs=[usage(), usage('future', finish=at(13)), tuple(old)])
        self.assertIn('consumed_success=1 / observed_success=1; observed_completions=1', text)

    def test_future_assessments_excluded_and_invalid_times_quarantined(self):
        self.save(state(entry()))
        self.assertIn('Evaluated assessments=0', self.report(at(9, 40), []))
        self.assertIn('after_cutoff=1', self.report(at(9, 40), []))
        item = entry()
        item['evaluated_at'] = '2026-09-04T09:46:10'  # naive
        self.save(state(item))
        self.assertIn('invalid_entries=1', self.report(pairs=[]))

    def test_integration_before_decision_actions_and_no_files_changed(self):
        from research.report_theta_audit import build_report
        self.save(state(entry()))
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        text = build_report(self.directory, DAY, 1, at(12))
        self.assertLess(text.index('SPX CONTEXT NOTIFICATIONS'), text.index('DECISION INTERPRETATION'))
        self.assertIn('Retained flow policy', text)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(ValueError):
            context_report(self.directory, DAY, DAY, at(12))
        with self.assertRaises(ValueError):
            context_report(self.directory, DAY, END, at(12).replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
