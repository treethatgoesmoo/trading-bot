"""Frozen, experimental price-only checklist; no orders or option-price model."""
VERSION = 'spx-action-retest-v1'
LABELS = {
    'consider_calls': 'CONSIDER CALLS — experimental',
    'consider_puts': 'CONSIDER PUTS — experimental',
    'wait_confirmation': 'WAIT FOR CONFIRMATION',
    'wait_retest': "WAIT FOR RETEST — DON'T CHASE",
    'wait_reversal': 'WAIT FOR REVERSAL CONFIRMATION',
    'no_trade': 'NO TRADE',
}
CANDIDATES = {'consider_calls': 'UP', 'consider_puts': 'DOWN'}


def build_action(context):
    """Only fresh, complete SPX structure + completed 5m geometry drive actions.

    Prior levels, indicative SPY and sampled flow never gate this version.
    A retest is the existing OHLC boundary revisit/close-outside description,
    not a limit fill, intrabar sequence guarantee, or newly detected reversal.
    """
    def state(key):
        return context.get(key, {}).get('state')

    fresh = context.get('fresh') is True
    direction = context.get('direction')
    setup, momentum = state('setup'), state('momentum')
    spx = context.get('sources', {}).get('spx', {})
    complete = (fresh and spx.get('used') is True and spx.get('status') == 'fresh'
                and state('structure') in ('inside_range', 'above_range', 'below_range'))
    code, reason = 'no_trade', 'incomplete_or_stale_spx'
    text = 'Required SPX evidence is stale, missing, or incomplete; do not act on the cached setup.'
    if complete:
        if setup in ('failed_up', 'failed_down') and direction == 'NEUTRAL':
            code, reason = 'wait_reversal', 'failed_attempt_not_opposite_entry'
            text = 'The previous attempt failed. Do not automatically switch option sides; wait for a new qualifying setup.'
        elif setup == 'inside_range' and direction == 'NEUTRAL':
            reason, text = 'inside_range', 'No directional breakout is established; stay out under this checklist.'
        elif direction in ('UP', 'DOWN') and state('structure') == {'UP': 'above_range', 'DOWN': 'below_range'}[direction]:
            suffix = direction.lower()
            if setup in ('breakout_' + suffix, 'reclaimed_' + suffix):
                code, reason = 'wait_confirmation', 'follow_through_unconfirmed'
                text = 'First or renewed boundary break; wait for follow-through rather than entering on this close.'
            elif setup in ('holding_' + suffix, 'retest_' + suffix):
                if momentum in (suffix + '_steady', suffix + '_strengthening'):
                    if setup.startswith('holding_'):
                        code, reason = 'wait_retest', 'aligned_hold_without_current_retest'
                        text = 'Closes hold outside the range with aligned momentum; wait for a boundary retest. No distance/extension claim is made.'
                    else:
                        code = 'consider_calls' if direction == 'UP' else 'consider_puts'
                        reason = 'retest_with_aligned_nonfading_momentum'
                        text = ('Price-only experimental paper candidate; confirmation is contextual, not an entry gate. '
                                'Before any manual purchase, check the actual 0DTE contract, '
                                'fresh bid/ask, spread, time to expiry and acceptable premium loss in Robinhood. No contract or fill is selected. '
                                + context.get('invalidation', {}).get('text', 'Recheck the range boundary.'))
                else:
                    reason, text = 'momentum_not_aligned_nonfading', 'Momentum is fading, mixed, opposed or unavailable; no entry qualifies.'
    roles = {'spx': 'rule_input' if complete else 'quality_gate'}
    for source in ('prior_spx', 'spy_proxy', 'options_flow'):
        roles[source] = 'contextual_only' if context.get('sources', {}).get(source, {}).get('used') is True else 'not_used'
    return dict(version=VERSION, state=code, label=LABELS[code], experimental=True,
                candidate_direction=CANDIDATES.get(code), reason=reason, source_roles=roles,
                text=LABELS[code] + '\n' + text)


def validated_action(context):
    """Validate recorded classification; never manufacture actions for old rows."""
    saved = context.get('action')
    if not isinstance(saved, dict) or saved.get('version') != VERSION:
        return None
    try:
        return saved if saved == build_action(context) else None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
