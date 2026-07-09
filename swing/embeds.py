"""Discord embeds for the swing options feature — pure builders, no I/O."""

import discord

from config import SWING_ENTRY_AVOID_OPEN_MINUTES
from swing.options import OptionContract
from swing.strategy import SwingSignal, SwingVote, entry_zone

_GREEN, _RED, _AMBER, _BLUE = 0x00CC66, 0xFF4444, 0xFFAA00, 0x3498DB


def _signal_lines(signals: dict[str, str], weights: dict[str, float]) -> str:
    lines = []
    for name, val in signals.items():
        icon = "\U0001f7e2" if val == "BULLISH" else ("\U0001f534" if val == "BEARISH" else "\U0001f7e1")
        w = weights.get(name, 0)
        lines.append(f"{icon} {name} (w:{w})")
    return " | ".join(lines)


def _contract_block(c: OptionContract) -> str:
    price = f"${c.mid:.2f}"
    if c.quote_stale:
        price += " (indicative — prior close quote)"
    lines = [
        f"**{c.contract_symbol}**",
        f"Expiry {c.expiry} ({c.dte} DTE) · strike ${c.strike:g}",
        f"Premium ~{price} · breakeven ${c.breakeven:.2f}",
        f"OI {c.open_interest:,} · vol {c.volume:,}"
        + (f" · IV {c.implied_vol * 100:.0f}%" if c.implied_vol else ""),
    ]
    if c.illiquid:
        lines.append(f"⚠️ Thin market (spread {c.spread_pct:.1f}%) — use limit orders, size down")
    return "\n".join(lines)


def _entry_plan_block(direction: str, zone_low: float, zone_high: float,
                      no_chase: float, invalidation: float | None) -> str:
    """Entry-timing guidance: the signal marks the day, not the minute."""
    is_call = direction == "CALL"
    chase_word = "above" if is_call else "below"
    inval_note = ""
    if invalidation is not None:
        inval_note = f" — thesis only breaks at ${invalidation:g}"
    return "\n".join([
        f"• Skip the first ~{SWING_ENTRY_AVOID_OPEN_MINUTES} min after open (widest spreads, worst fills).",
        f"• Ideal underlying zone: **${zone_low:g} – ${zone_high:g}** — a dip against you here is a "
        f"normal wiggle and a better fill{inval_note}.",
        f"• Don't chase {chase_word} ${no_chase:g}: quoted premium/breakeven no longer apply — "
        f"wait for a pullback into the zone.",
        "• Enter with a limit at the option mid, never market.",
    ])


def swing_signal_embed(sig: SwingSignal) -> discord.Embed:
    is_call = sig.direction == "CALL"
    color = _GREEN if is_call else _RED
    arrow = "⬆️" if is_call else "⬇️"
    embed = discord.Embed(
        title=f"{arrow} SWING SIGNAL: BUY {sig.ticker} {sig.direction}S (1-2 wk)",
        color=color,
    )
    v = sig.vote
    embed.add_field(name="Underlying", value=f"${v.close}", inline=True)
    embed.add_field(name="Confidence", value=f"{v.confidence}% (score {v.score:+.1f})", inline=True)
    embed.add_field(name="Daily RSI", value=f"{v.rsi_d}" if v.rsi_d is not None else "N/A", inline=True)
    embed.add_field(name="Invalidation",
                    value=f"${sig.invalidation} (close {'below' if is_call else 'above'} = exit)",
                    inline=True)
    embed.add_field(name="Target", value=f"{'+' if is_call else '-'}{sig.target_pct:g}% underlying", inline=True)
    embed.add_field(name="Horizon", value=f"{sig.horizon_days} days", inline=True)

    if sig.contract is not None:
        embed.add_field(name="Suggested contract", value=_contract_block(sig.contract), inline=False)
    else:
        embed.add_field(name="Suggested contract",
                        value="Contract lookup failed — retry with `!swing " + sig.ticker + "`",
                        inline=False)

    if sig.entry_zone_low or sig.entry_zone_high:
        embed.add_field(
            name="⏱️ Entry plan",
            value=_entry_plan_block(sig.direction, sig.entry_zone_low, sig.entry_zone_high,
                                    sig.no_chase, sig.invalidation),
            inline=False)

    embed.add_field(name="Signals", value=_signal_lines(v.signals, v.weights), inline=False)
    embed.set_footer(text=f"Swing scan {sig.signal_date} · graded after the horizon · not financial advice")
    return embed


def swing_analysis_embed(ticker: str, vote: SwingVote,
                         contract: OptionContract | None) -> discord.Embed:
    """On-demand !swing readout — shown even when below signal thresholds."""
    if vote.direction == "BULLISH":
        color, label = _GREEN, "LEANS CALLS"
    elif vote.direction == "BEARISH":
        color, label = _RED, "LEANS PUTS"
    else:
        color, label = _AMBER, "NEUTRAL"
    embed = discord.Embed(title=f"\U0001f4c8 Swing Analysis — {ticker}: {label}", color=color)
    embed.add_field(name="Underlying", value=f"${vote.close}", inline=True)
    embed.add_field(name="Confidence", value=f"{vote.confidence}% (score {vote.score:+.1f})", inline=True)
    embed.add_field(name="Daily RSI", value=f"{vote.rsi_d}" if vote.rsi_d is not None else "N/A", inline=True)
    embed.add_field(name="SMA 50/200", value=f"${vote.sma50} / ${vote.sma200}", inline=True)
    embed.add_field(name="52w High", value=f"${vote.high_52w}", inline=True)
    embed.add_field(name="ATR(14)", value=f"${vote.atr14}", inline=True)
    if contract is not None:
        embed.add_field(name="Nearest 1-2wk contract", value=_contract_block(contract), inline=False)
    if vote.direction in ("BULLISH", "BEARISH") and vote.atr14 > 0:
        direction = "CALL" if vote.direction == "BULLISH" else "PUT"
        zone_low, zone_high, no_chase = entry_zone(direction, vote.close, vote.atr14)
        embed.add_field(
            name="⏱️ Entry plan",
            value=_entry_plan_block(direction, zone_low, zone_high, no_chase, None),
            inline=False)
    embed.add_field(name="Signals", value=_signal_lines(vote.signals, vote.weights), inline=False)
    embed.set_footer(text="On-demand analysis — not tracked or graded")
    return embed


def swing_check_embed(sig: dict, last_price: float, status: str) -> discord.Embed:
    """Midday confirmation / invalidation ping for an open signal."""
    if status == "TARGET":
        color, head = _GREEN, f"✅ Swing target hit — {sig['ticker']} {sig['direction']}"
    elif status == "INVALIDATED":
        color, head = _RED, f"❌ Swing invalidated — {sig['ticker']} {sig['direction']}"
    else:
        color, head = _BLUE, f"⏳ Swing check — {sig['ticker']} {sig['direction']}"
    embed = discord.Embed(title=head, color=color)
    entry = float(sig["entry_price"])
    move = (last_price - entry) / entry * 100
    embed.add_field(name="Now", value=f"${last_price:.2f} ({move:+.2f}%)", inline=True)
    embed.add_field(name="Entry ref", value=f"${entry:.2f} ({sig['signal_date']})", inline=True)
    embed.add_field(name="Invalidation", value=f"${sig['invalidation']}", inline=True)
    if status == "TARGET":
        embed.add_field(name="Suggestion", value="Consider taking profits / trailing the rest.", inline=False)
    elif status == "INVALIDATED":
        embed.add_field(name="Suggestion", value="Thesis voided — consider cutting the position.", inline=False)
    embed.set_footer(text="Final grading happens at the next pre-open pass")
    return embed


def swing_grade_embed(outcome: dict) -> discord.Embed:
    win = outcome["win"]
    color = _GREEN if win else _RED
    icon = "✅" if win else "❌"
    embed = discord.Embed(
        title=f"{icon} Swing graded: {outcome['ticker']} {outcome['direction']} — {outcome['resolution']}",
        color=color,
    )
    embed.add_field(name="Signal", value=f"{outcome['signal_date']} @ ${outcome['entry_price']}", inline=True)
    embed.add_field(name="Resolved", value=f"{outcome['resolution_date']} ({outcome['move_pct']:+.2f}%)", inline=True)
    embed.add_field(name="Confidence", value=f"{outcome['confidence']}%", inline=True)
    opt = outcome.get("option")
    if opt:
        method = "requoted" if opt["pnl_method"] == "requote" else "delta estimate"
        embed.add_field(
            name="Option P&L (est.)",
            value=f"{opt['pnl_pct']:+.1f}% on ~${opt['entry_mid']:.2f} premium ({method})",
            inline=False,
        )
    embed.set_footer(text="Feeds swing stats + weight tuning (tune_swing_weights.py)")
    return embed
