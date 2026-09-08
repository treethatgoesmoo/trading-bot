"""Frozen checklist behavior, not profitability tests."""
from copy import deepcopy
from datetime import timedelta
import unittest

from test_spx_context import at, bar, record
from research.zero_dte.spx_actions import build_action, validated_action, VERSION
from research.zero_dte.spx_context import build_context


def candidate(direction='UP'):
    extra = []
    for i in range(10):
        o, h, l, c = (100, 104, 100, 102) if i < 5 else (102, 106, 102, 104)
        if i == 9:
            o, h, l, c = 104, 106, 101, 105
        if direction == 'DOWN':
            o, h, l, c = 200-o, 200-l, 200-h, 200-c
        extra.append(bar(at(9, 45) + timedelta(minutes=i), o, h, l, c))
    return build_context(record(extra), [], at(9, 55, 10))


class ActionTests(unittest.TestCase):
    def test_real_completed_bars_symmetric_candidates(self):
        for side, code in [('UP', 'consider_calls'), ('DOWN', 'consider_puts')]:
            with self.subTest(side=side):
                ctx = candidate(side)
                self.assertEqual(ctx['action']['state'], code)
                self.assertEqual(ctx['action']['candidate_direction'], side)
                self.assertEqual(ctx['action']['version'], VERSION)
                self.assertEqual(ctx['signature'][-1], ctx['invalidation']['state'])
                self.assertEqual(ctx['signature'][-2], code)
                self.assertIn('Robinhood', ctx['action']['text'])
                self.assertEqual(validated_action(ctx), ctx['action'])

    def test_fading_mixed_opposed_and_missing_momentum_never_candidate(self):
        for momentum in ['up_fading', 'mixed', 'down_strengthening', 'unavailable', None]:
            ctx = candidate()
            ctx['momentum']['state'] = momentum
            action = build_action(ctx)
            self.assertEqual(action['state'], 'no_trade')
            self.assertIsNone(action['candidate_direction'])

    def test_missing_stale_incomplete_and_contradictory_structure_fail_closed(self):
        for change in [{'fresh': False}, {'structure': {'state': 'incomplete', 'text': 'Incomplete'}},
                       {'sources': {}}, {'structure': {'state': 'below_range', 'text': 'Below'}}]:
            ctx = candidate()
            ctx.update(change)
            self.assertEqual(build_action(ctx)['state'], 'no_trade')
            self.assertIsNone(validated_action(ctx))

    def test_state_transitions_are_not_implicit_reversal_or_extension_signals(self):
        for side in ('UP', 'DOWN'):
            for prefix, expected in [('breakout', 'wait_confirmation'), ('reclaimed', 'wait_confirmation'),
                                     ('holding', 'wait_retest'), ('retest', 'consider_calls' if side == 'UP' else 'consider_puts')]:
                ctx = candidate(side)
                ctx['setup']['state'] = prefix + '_' + side.lower()
                self.assertEqual(build_action(ctx)['state'], expected)
        ctx = candidate()
        ctx['direction'] = 'NEUTRAL'
        ctx['structure']['state'] = 'inside_range'
        for setup in ('failed_up', 'failed_down'):
            ctx['setup']['state'] = setup
            action = build_action(ctx)
            self.assertEqual(action['state'], 'wait_reversal')
            self.assertIsNone(action['candidate_direction'])
        ctx['setup']['state'] = 'inside_range'
        self.assertEqual(build_action(ctx)['state'], 'no_trade')

    def test_confirmations_never_gate_actions_or_earn_action_input_credit(self):
        ctx = candidate()
        original = ctx['action']
        for source in ('options_flow', 'spy_proxy', 'prior_spx'):
            ctx['sources'][source] = {'used': True, 'status': 'opposed'}
        action = build_action(ctx)
        self.assertEqual(action['state'], original['state'])
        self.assertEqual(action['reason'], original['reason'])
        self.assertEqual(action['source_roles'], dict(spx='rule_input', prior_spx='contextual_only',
                                                     spy_proxy='contextual_only', options_flow='contextual_only'))

    def test_old_unknown_or_tampered_actions_not_retrofitted(self):
        ctx = candidate()
        for mutation in ('missing', 'version', 'state', 'source_roles'):
            bad = deepcopy(ctx)
            if mutation == 'missing':
                bad.pop('action')
            else:
                bad['action'][mutation] = 'bad'
            self.assertIsNone(validated_action(bad))
