"""Discord embeds for the 0DTE (intraday forecast) feature — pure builders, no I/O.

(Embeds for morning bias, first-candle range, pre-market levels,
break-and-retest trade signals/outcomes, the daily summary, VIX regime/spike
pings, and candlestick patterns were removed along with those live features
— see docs/state-of-the-code.md.)

**2026-08-15: the "Confidence %" field and the nightly Forecast Report Card
(`forecast_results_embed`, `ReportDetailsView`) were removed from what the
bot posts** — both presented a number that looked like a calibrated
probability but wasn't: stated confidence 70-100% realized ~53% hit rate
across 810 calls, and the bare "Bot Accuracy X/Y (Z%)" report card had no
baseline next to it, so a 55% day read as a win when it was at or below the
always-BULLISH baseline for that stream (see docs/strategy-log.md). The
backend grading this fed (`zero_dte.tracker.evaluate_forecasts`,
`data/results-*.json`, `tune_weights.py`) remains enabled — that's the
evidence pipeline that caught the confidence problem in the first place.
New schema-v2 rows use signal-time ATR and analytics exclude older schemas;
only the misleading live display was cut. Momentum/Extension fields added
the same day are explicitly descriptive (see zero_dte/strategy.py), not a
replacement claim of accuracy.
"""

import discord

from zero_dte.strategy import DirectionForecast


def momentum_label(score: float) -> str:
    if score >= 60:
        return "Strong upward"
    if score > 15:
        return "Upward"
    if score >= -15:
        return "Flat / weak"
    if score > -60:
        return "Downward"
    return "Strong downward"


def extension_label(score: float) -> str:
    if score >= 65:
        return "High — reversal risk, not confirmation"
    if score >= 35:
        return "Moderate"
    return "Low"


def _direction_text(direction: str | None) -> str:
    if direction is None:
        return "Unavailable"
    if direction == "VOLATILE":
        return "Mixed / choppy"
    return direction.title()


def forecast_embed(fc: DirectionForecast) -> discord.Embed:
    if fc.direction == "BULLISH":
        color, emoji = 0x00CC66, "\U0001f7e2"
    elif fc.direction == "BEARISH":
        color, emoji = 0xFF4444, "\U0001f534"
    else:
        color, emoji = 0xFFAA00, "\U0001f7e1"

    title_direction = _direction_text(fc.direction).upper()
    embed = discord.Embed(
        title=f"{emoji} {fc.ticker} Direction: {title_direction} — next {fc.interval} minutes",
        color=color,
    )

    forecast_text = _direction_text(fc.direction)
    if fc.direction == "VOLATILE" and fc.lean:
        forecast_text += f" (leans {_direction_text(fc.lean).lower()})"
    timeframe_lines = [f"**{fc.interval}m forecast:** {forecast_text}"]
    if fc.interval < 30:
        timeframe_lines.append(f"**30m completed candle:** {_direction_text(fc.candle_30m_direction)}")
    if fc.interval < 60:
        timeframe_lines.append(f"**1h completed candle:** {_direction_text(fc.candle_1h_direction)}")
    embed.add_field(name="Timeframes", value="\n".join(timeframe_lines), inline=False)

    state_lines = []
    if fc.momentum_score is not None:
        state_lines.append(
            f"**Momentum:** {momentum_label(fc.momentum_score)} ({fc.momentum_score:+.0f})"
        )
    else:
        state_lines.append("**Momentum:** Not enough session data")
    if fc.extension_score is not None:
        state_lines.append(
            f"**Stretch:** {extension_label(fc.extension_score)} ({fc.extension_score:.0f})"
        )
    else:
        state_lines.append("**Stretch:** Not enough session data")
    state_lines.append("**Turning point:** Not confirmed")
    embed.add_field(name="Market Read", value="\n".join(state_lines), inline=False)

    embed.set_footer(text=f"Forecast at {fc.timestamp} · descriptive context is not a probability")
    return embed


# forecast_results_embed (the nightly "Forecast Report Card") was removed
# 2026-08-15 — see the module docstring above. Backend grading
# (zero_dte.tracker.evaluate_forecasts -> data/results-*.json) remains active;
# only the live-posted embed and its bare accuracy number are gone. Use
# dashboard.py or !stats (core.analytics, which shows baselines alongside
# accuracy) to review graded history instead.


def bb_alert_embed(alert) -> discord.Embed:
    """A Bollinger band touch ping (1m/5m upper or lower band)."""
    from zero_dte.bb_watcher import BB_TF_LABEL
    if alert.band == "upper":
        color, arrow, side = 0xFF4444, "⬆️", "Upper"   # stretched high → mean-reversion risk down
    else:
        color, arrow, side = 0x00CC66, "⬇️", "Lower"
    tf = BB_TF_LABEL[alert.timeframe]
    embed = discord.Embed(
        title=f"\U0001f3af {arrow} Bollinger {side} Band — {alert.ticker} ({tf})",
        color=color,
    )
    embed.add_field(name="Signal", value=alert.message, inline=False)
    embed.add_field(name="Close", value=f"${alert.price}", inline=True)
    embed.add_field(name=f"{side} band", value=f"${alert.band_value}", inline=True)
    embed.add_field(name="Mid (SMA)", value=f"${alert.middle}", inline=True)
    embed.set_footer(text=f"Band touch at {alert.bar_time} — stretched, not a direction call; confirm with price action")
    return embed


def volume_alert_embed(alert) -> discord.Embed:
    """A relative-volume spike ping (5m bar clearing the spike threshold)."""
    if alert.direction == "BEARISH":
        color, arrow = 0xFF4444, "⬇️"
    elif alert.direction == "BULLISH":
        color, arrow = 0x00CC66, "⬆️"
    else:
        color, arrow = 0x999999, "➖"
    embed = discord.Embed(
        title=f"\U0001f4ca {arrow} Volume Spike — {alert.ticker} (5m)",
        color=color,
    )
    embed.add_field(name="Signal", value=alert.message, inline=False)
    embed.add_field(name="Close", value=f"${alert.price}", inline=True)
    embed.add_field(name="Ratio", value=f"{alert.ratio:.1f}x avg", inline=True)
    embed.add_field(name="Volume", value=f"{alert.volume:,.0f}", inline=True)
    embed.set_footer(text=f"Spike at {alert.bar_time} — elevated activity, not a direction call; confirm with price action")
    return embed
