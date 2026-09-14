"""Fictional, isolated examples rendered through the production embed layout."""
from spy_setup.embeds import snapshot_embed


DEMO = 'DEMO — NOT A TRADE SIGNAL'


def preview_embeds():
    """Return five examples; no clock, provider, ledger or live snapshot access."""
    examples = (
        ('forming', 'retest', 'Fictional retest held. Await continuation; this is NOT an entry.', '14:48'),
        ('triggered', 'active', 'Fictional continuation confirmed. No real trade or fill occurred.', '14:50'),
        ('invalidated', 'terminal', 'Alternative ending: fictional SPY invalidation touched.', '14:53'),
        ('target_reached', 'terminal', 'Alternative ending: fictional SPY reference target touched; not an option return.', '14:54'),
        ('expired', 'terminal', 'Alternative ending: scheduled expiry; no position exit is assumed.', '20:55'),
    )
    result = []
    # Fixed winter date: timestamps display as ET, never as current market evidence.
    for lifecycle, phase, reason, clock in examples:
        active = lifecycle != 'forming'
        snapshot = {
            'fresh': True,
            'opening_range': {'low': 599.0, 'high': 600.0},
            'vwap': 599.8, 'relative_volume': 1.5, 'volume_samples': 5,
            'classification': {
                'available': True, 'setup_id': 'fictional-preview',
                'direction': 'bullish', 'lifecycle': lifecycle, 'phase': phase,
                'reasons': [reason], 'trigger': 600.21, 'invalidation': 599.99,
                'entry_reference': 600.30 if active else None,
                'target': 600.92 if active else None,
                'event_at': f'2026-01-06T{clock}:00+00:00',
                'expires_at': '2026-01-06T20:55:00+00:00' if active else '2026-01-06T15:01:00+00:00',
            },
        }
        embed = snapshot_embed(snapshot)
        embed.title = f'{DEMO}\n{embed.title}'
        embed.description = ('Fictional SPY prices and example times. NOT current market data.\n'
                             + embed.description)
        embed.set_footer(text=f'{DEMO} • Illustrative only; no live state changed.')
        result.append(embed)
    return result
