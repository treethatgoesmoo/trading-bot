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


def forecast_embed(fc: DirectionForecast) -> discord.Embed:
    if fc.direction == "BULLISH":
        color, emoji = 0x00CC66, "\U0001f7e2"
    elif fc.direction == "BEARISH":
        color, emoji = 0xFF4444, "\U0001f534"
    else:  # NEUTRAL / VOLATILE
        color, emoji = 0xFFAA00, "\U0001f7e1"

    title_dir = "VOLATILE ⚠️" if fc.direction == "VOLATILE" else fc.direction
    embed = discord.Embed(
        title=f"{emoji} {fc.interval}-Min Forecast: {title_dir} {fc.ticker}",
        color=color,
    )
    if fc.direction == "VOLATILE":
        embed.description = ("Indicators are contradicting each other — expect chop. "
                             "Low-conviction read; caution on entries.")
        if fc.lean:
            lean_icon = "\U0001f7e2" if fc.lean == "BULLISH" else "\U0001f534"
            embed.add_field(name="Lean", value=f"{lean_icon} {fc.lean}", inline=True)
    embed.add_field(name="Price", value=f"${fc.current_price}", inline=True)
    if fc.vwap:
        pos = "above" if fc.current_price > fc.vwap else "below"
        embed.add_field(name="VWAP", value=f"${fc.vwap} ({pos})", inline=True)
    if fc.rsi_30m:
        embed.add_field(name="RSI", value=f"{fc.rsi_30m}", inline=True)
    if fc.ema_direction:
        embed.add_field(name="EMA 9/21", value=fc.ema_direction, inline=True)
    if fc.macd_direction:
        embed.add_field(name="MACD", value=fc.macd_direction, inline=True)
    if fc.divergence:
        div_str = "Bearish divergence" if fc.divergence == "BEARISH_DIV" else "Bullish divergence"
        tf = f" ({fc.divergence_tf})" if fc.divergence_tf else ""
        embed.add_field(name="Divergence", value=f"⚠️ {div_str}{tf}", inline=True)
    if fc.rsi_caution:
        embed.add_field(name="RSI Watch", value=f"⚠️ {fc.rsi_caution}", inline=False)

    # Descriptive-only readout (NOT graded, NOT the same claim "Confidence"
    # used to make — see the module docstring and zero_dte/strategy.py).
    if fc.momentum_score is not None or fc.extension_score is not None:
        parts = []
        if fc.momentum_score is not None:
            arrow = "\U0001f4c8" if fc.momentum_score > 15 else ("\U0001f4c9" if fc.momentum_score < -15 else "\u2192")
            parts.append(f"{arrow} Momentum {fc.momentum_score:+.0f}")
        if fc.extension_score is not None:
            tag = "stretched" if fc.extension_score >= 65 else ("mid" if fc.extension_score >= 35 else "fresh")
            parts.append(f"Extension {fc.extension_score:.0f} ({tag})")
        embed.add_field(name="Momentum / Extension (descriptive, not graded)",
                        value=" · ".join(parts), inline=False)

    # Day context: the ACTUAL session range so far (replaced the projected
    # day forecast — tracked fact, not an estimate).
    if fc.day_high is not None and fc.day_low is not None:
        embed.add_field(
            name="Today so far",
            value=f"High ${fc.day_high} · Low ${fc.day_low}",
            inline=False,
        )

    # Signal breakdown
    sig_lines = []
    for name, val in fc.signals.items():
        w = fc.weights.get(name, 0)
        icon = "\U0001f7e2" if val == "BULLISH" else ("\U0001f534" if val == "BEARISH" else "\U0001f7e1")
        sig_lines.append(f"{icon} {name} (w:{w})")
    if sig_lines:
        embed.add_field(name="Signals", value=" | ".join(sig_lines), inline=False)

    embed.set_footer(text=f"Forecast at {fc.timestamp} | Next {fc.interval} min outlook")
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
