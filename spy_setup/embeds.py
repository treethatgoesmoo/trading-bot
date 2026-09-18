"""Private Discord setup/status rendering; all levels explicitly refer to SPY."""
from datetime import timedelta
import math
import discord
from zoneinfo import ZoneInfo
from core.session_bars import utc
from spy_setup.signals import VERSION


def _number(value):
    return f'{float(value):.2f}' if value is not None and math.isfinite(float(value)) else 'not set'


def display_time(value, *, include_date=False):
    """Pacific display only; strategy clocks and stored timestamps are unchanged."""
    pattern = '%Y-%m-%d %I:%M:%S %p %Z' if include_date else '%I:%M %p %Z'
    return utc(value).astimezone(ZoneInfo('America/Los_Angeles')).strftime(pattern) if value else 'not set'


def snapshot_embed(snapshot, *, context=None):
    classification = snapshot.get('classification', {})
    available = snapshot.get('fresh') and classification.get('available')
    lifecycle = classification.get('lifecycle', 'unavailable') if available else 'unavailable'
    direction = classification.get('direction') if available else None
    side = 'CALL setup' if direction == 'bullish' else 'PUT setup' if direction == 'bearish' else 'No setup'
    if available and classification.get('kind') == 'failed_breakdown_reversal':
        side += ' • Failed-breakdown reversal'
    title = f'SPY signal → SPX 0DTE • {side} • {lifecycle.replace("_", " ").upper()}'
    color = (0x2ECC71 if direction == 'bullish' else 0xE74C3C if direction == 'bearish' else 0x808080)
    if available:
        bias = '🟢 ↑ BULLISH setup' if direction == 'bullish' else '🔴 ↓ BEARISH setup' if direction == 'bearish' else '⚪ NO CONFIRMED SETUP'
        close = snapshot.get('timeframes', {}).get('1', {}).get('close')
        vwap = snapshot.get('vwap')
        momentum = ''
        if close is not None and vwap is not None:
            position = 'above' if close > vwap else 'below' if close < vwap else 'at'
            momentum = f'\nLatest close {position} VWAP | RVOL {_number(snapshot.get("relative_volume"))}x'
        description = f'**{bias}**{momentum}\n' + '\n'.join(classification.get('reasons', []))
        if classification.get('bar_at'):
            description += '\nLatest completed SPY minute: ' + display_time(utc(classification['bar_at']) + timedelta(minutes=1))
    else:
        title, color = 'SPY signal → SPX 0DTE • Unavailable', 0x808080
        description = 'No current setup claim — ' + str(snapshot.get('reason', 'unavailable')).replace('_', ' ') + '.'
    embed = discord.Embed(title=title, description=description[:4096], color=color)
    if available and classification.get('setup_id'):
        embed.add_field(name='SPY levels', inline=False,
                        value=f"Continuation close trigger: {_number(classification.get('trigger'))}\n"
                              f"Invalidation touch: {_number(classification.get('invalidation'))}\n"
                              f"Confirmed close reference: {_number(classification.get('entry_reference'))}\n"
                              f"2R reference target: {_number(classification.get('target'))}\n"
                              f"Phase: {classification.get('phase')} | Event: {display_time(classification.get('event_at'))}\n"
                              f"Expiry: {display_time(classification.get('expires_at'))}")
    if available:
        opening = snapshot.get('opening_range', {})
        embed.add_field(name='SPY context', inline=False,
                        value=f"Opening range: {_number(opening.get('low'))}–{_number(opening.get('high'))}\n"
                              f"VWAP: {_number(snapshot.get('vwap'))} | Same-minute relative volume: {_number(snapshot.get('relative_volume'))}x\n"
                              f"Baseline samples: {snapshot.get('volume_samples', 0)} / 5 preceding sessions")
    if context is not None:
        from spy_setup.market_context import context_text
        embed.add_field(name='SPY momentum & BB readiness', value=context_text(context), inline=False)
    operational = snapshot.get('operational')
    if isinstance(operational, dict):
        operational = ' | '.join(f'{key.replace("_", " ")}: {operational[key]}' for key in
                                ('health', 'collection_enabled', 'publication_enabled', 'poll_status') if key in operational)
    if isinstance(operational, str) and operational:
        embed.add_field(name='Service', value=operational[:1024], inline=False)
    embed.set_footer(text=f'{VERSION} • Closed-candle references • Unvalidated rules')
    return embed
