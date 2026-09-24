"""Live alerts stay glanceable; detailed command/replay renderers stay separate."""
from copy import deepcopy

import pytest

from spy_setup.embeds import alert_embed, snapshot_embed
from spy_setup.informational_delivery import informational_embed


def snapshot(lifecycle, direction='bullish'):
    return {'fresh': True, 'classification': {
        'available': True, 'direction': direction, 'lifecycle': lifecycle,
        'setup_id': 'example', 'phase': 'retest',
        'event_at': '2026-09-24T16:14:00+00:00',
        'bar_at': '2026-09-24T16:13:00+00:00',
        'expires_at': '2026-09-24T16:29:00+00:00',
        'trigger': 765.80, 'entry_reference': 765.86,
        'invalidation': 765.20, 'target': 767.18}}


def compact(embed):
    assert not embed.fields
    assert not embed.footer.text
    assert '\n' not in embed.description
    assert len(embed.title) < 100
    assert len(embed.description) < 200


@pytest.mark.parametrize('direction,side', [('bullish', 'CALL'), ('bearish', 'PUT')])
@pytest.mark.parametrize('lifecycle,detail', [
    ('forming', 'Retest confirmed · Not triggered'),
    ('triggered', 'Ref $765.86 · Invalid $765.20 · Target $767.18'),
    ('invalidated', '$765.20 level breached'),
    ('target_reached', '$767.18 reference reached'),
    ('expired', 'Setup window ended'),
])
def test_live_setup_lifecycles_are_compact_and_preserve_references(direction, side, lifecycle, detail):
    data = snapshot(lifecycle, direction)
    before = deepcopy(data)
    embed = alert_embed(data)
    compact(embed)
    assert f'SPY {side} SETUP · {lifecycle.replace("_", " ").upper()}' in embed.title
    assert embed.description == f'{detail} · 09:14 AM PDT'
    assert data == before
    if lifecycle == 'invalidated':
        assert embed.color.value == 0xE74C3C


def test_reversal_kind_remains_distinguishable():
    data = snapshot('forming')
    data['classification']['kind'] = 'failed_breakdown_reversal'
    embed = alert_embed(data)
    compact(embed)
    assert embed.description.startswith('Failed-breakdown reversal · ')


@pytest.mark.parametrize('fresh,available', [(False, True), (True, False), (False, False)])
def test_unavailable_alert_does_not_expose_actionable_levels(fresh, available):
    data = snapshot('triggered')
    data['fresh'] = fresh
    data['classification']['available'] = available
    embed = alert_embed(data)
    compact(embed)
    assert embed.title == '⚪ SPY UNAVAILABLE'
    assert '$' not in embed.description
    assert 'CALL' not in embed.title


def test_detailed_snapshot_still_has_levels_context_and_event_times():
    data = snapshot('triggered')
    embed = snapshot_embed(data)
    fields = {field.name: field.value for field in embed.fields}
    assert 'Confirmed close reference: 765.86' in fields['SPY levels']
    assert 'Expiry: 09:29 AM PDT' in fields['SPY levels']
    assert 'SPY context' in fields
    assert 'Unvalidated rules' in embed.footer.text


@pytest.mark.parametrize('direction,icon', [('above', '🟢'), ('below', '🔴')])
def test_opening_cross_headline_is_a_level_event_not_persistent_bias(direction, icon):
    event = dict(kind=f'opening_range_cross_{direction}', direction=direction,
                 timeframe='1', close=765.86, previous_close=764.62,
                 event_at='2026-09-24T16:16:00+00:00')
    before = deepcopy(event)
    embed = informational_embed(event)
    compact(embed)
    assert embed.title == f'{icon} SPY {direction.upper()} OPENING RANGE'
    assert embed.description == '$765.86 · 1m +1.24 · 09:16 AM PDT'
    assert event == before
