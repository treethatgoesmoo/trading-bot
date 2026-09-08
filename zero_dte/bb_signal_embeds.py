"""Compact Discord rendering of observed BB events; no trade recommendations."""
from datetime import timedelta

import discord

from core import market_calendar
from zero_dte.orb_audit import utc


def _number(value, digits=2, signed=False):
    if value is None:
        return 'unavailable'
    return f'{value:+.{digits}f}' if signed else f'{value:.{digits}f}'


def snapshot_embed(snapshot):
    events = snapshot.get('events', [])
    fresh = snapshot.get('fresh', False)
    description = ('; '.join(f"{event['timeframe']}m {event['kind'].replace('_', ' ')}" for event in events)
                   if fresh and events else 'No new completed-candle band event.' if fresh else
                   f"Data unavailable: {snapshot.get('reason', 'unknown').replace('_', ' ')}.")
    embed = discord.Embed(title='SPX • Bollinger events', description=description,
                          color=0x3498DB if fresh else 0x808080)
    for tf in ('1', '5', '15'):
        frame = snapshot.get('timeframes', {}).get(tf, {})
        if not fresh or not frame.get('available'):
            reason = frame.get('reason', snapshot.get('reason', 'unavailable')).replace('_', ' ')
            if frame.get('required_bars'):
                reason += f" ({frame.get('history_bars', 0)}/{frame['required_bars']} completed candles)"
            value = f'Unavailable — {reason}.'
        else:
            ended = (utc(frame['bar_at']) + timedelta(minutes=int(tf))).astimezone(market_calendar.ET)
            value = (f"Closed {ended:%H:%M} ET • {frame['state'].replace('_', ' ')}\n"
                     f"Price {_number(frame['close'])} | BB {_number(frame['lower'])} / "
                     f"{_number(frame['middle'])} / {_number(frame['upper'])}\n"
                     f"RSI {_number(frame['rsi'], 1)} (Δ {_number(frame['rsi_change'], 1, True)})\n"
                     f"MACD hist {_number(frame['macd_histogram'], 3, True)} "
                     f"(Δ {_number(frame['macd_change'], 3, True)})")
        embed.add_field(name=f'{tf}m', value=value, inline=False)
    embed.set_footer(text='Completed bars • BB(20,2), RSI(14), MACD(12,26,9) • Observations, not buy/sell signals.')
    return embed
