"""Discord rendering of completed-candle observations, never option advice."""
from datetime import timedelta
import math

import discord

from core import market_calendar
from core.session_bars import utc
from market_state.signals import VERSION


def _number(value, digits=2, signed=False):
    if value is None or not math.isfinite(float(value)):
        return 'unavailable'
    return f'{value:+.{digits}f}' if signed else f'{value:.{digits}f}'


def snapshot_embed(snapshot):
    """Render status or a transition; delivery eligibility belongs to runtime.

    Optional `operational` text/dict is displayed as a compact status field.
    Optional `transition.previous_direction` identifies neutral bias clearing.
    Missing/stale input cannot inherit a colored directional title.
    """
    classification = snapshot.get('classification', {})
    fresh = snapshot.get('fresh', False)
    available = fresh and classification.get('available', False)
    direction = classification.get('direction', 'unavailable') if available else 'unavailable'
    volatile = available and classification.get('volatile') is True
    transition = snapshot.get('transition', {})
    previous = transition.get('previous_direction') if isinstance(transition, dict) else None
    if direction in ('bullish', 'bearish'):
        title = f'SPX • {direction.upper()}' + (' + VOLATILE' if volatile else '')
        color = 0xF1C40F if volatile else 0x2ECC71 if direction == 'bullish' else 0xE74C3C
    elif direction == 'neutral':
        title = 'SPX • VOLATILE — no directional bias' if volatile else 'SPX • NEUTRAL'
        if previous in ('bullish', 'bearish'):
            title += ' — bias cleared'
        color = 0xF1C40F if volatile else 0x95A5A6
    else:
        title, color = 'SPX • State unavailable', 0x808080
        available = False
    if available:
        ended = (utc(classification['bar_at']) + timedelta(minutes=5)).astimezone(market_calendar.ET)
        description = f'5m candle completed {ended:%Y-%m-%d %H:%M} ET.\n'
        description += '\n'.join(classification.get('reasons', []))
    else:
        reason = snapshot.get('reason') if not fresh else classification.get('reason')
        description = f"No current state claim — {(reason or 'unavailable').replace('_', ' ')}."
    embed = discord.Embed(title=title, description=description[:4096], color=color)
    for tf in ('1', '5', '15'):
        frame = snapshot.get('timeframes', {}).get(tf, {})
        if not fresh or not frame.get('available'):
            reason = (frame.get('reason') or snapshot.get('reason') or 'unavailable').replace('_', ' ')
            if frame.get('required_bars'):
                reason += f" ({frame.get('history_bars', 0)}/{frame['required_bars']} candles)"
            value = f'Unavailable — {reason}.'
        else:
            ended = (utc(frame['bar_at']) + timedelta(minutes=int(tf))).astimezone(market_calendar.ET)
            value = (f"Closed {ended:%H:%M} ET • {frame['state'].replace('_', ' ')}\n"
                     f"Price {_number(frame['close'])} | BB {_number(frame['lower'])} / "
                     f"{_number(frame['middle'])} / {_number(frame['upper'])}\n"
                     f"RSI {_number(frame['rsi'], 1)} (Δ {_number(frame['rsi_change'], 1, True)}) | "
                     f"MACD hist {_number(frame['macd_histogram'], 3, True)} "
                     f"(Δ {_number(frame['macd_change'], 3, True)})")
        label = f'{tf}m' + (' • context only' if tf == '1' else ' • directional context' if tf == '15' else ' • state')
        embed.add_field(name=label, value=value, inline=False)
    operational = snapshot.get('operational')
    if operational:
        if isinstance(operational, dict):
            # Runtime may carry more metadata; do not dump arbitrary fields.
            operational = ' | '.join(f'{k.replace("_", " ")}: {operational[k]}' for k in
                                     ('health', 'collection_enabled', 'publication_enabled', 'poll_status')
                                     if k in operational)
        if isinstance(operational, str) and operational:
            embed.add_field(name='Service', value=operational[:1024], inline=False)
    embed.set_footer(text=f'{VERSION} • Completed candles • Observations, not predictions or buy/sell instructions.')
    return embed
