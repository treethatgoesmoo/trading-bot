"""Discord embeds for the 0DTE (intraday forecast) feature — pure builders, no I/O.

(Embeds for morning bias, first-candle range, pre-market levels,
break-and-retest trade signals/outcomes, the daily summary, VIX regime/spike
pings, and candlestick patterns were removed along with those live features
— see docs/state-of-the-code.md.)
"""

from datetime import datetime

import discord
import pytz

from config import FORECAST_INTERVAL_MINUTES
from zero_dte.strategy import DirectionForecast

ET = pytz.timezone("US/Eastern")


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
    embed.add_field(name="Confidence", value=f"{fc.confidence}%", inline=True)
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


def forecast_results_embed(ticker: str, date_str: str, results: list[dict],
                           user_score: tuple[int, int] | None = None) -> discord.Embed:
    scored = [r for r in results if r.get("correct") is not None]
    correct = sum(1 for r in scored if r["correct"])
    total = len(scored)
    accuracy = correct / total * 100 if total else 0

    if accuracy >= 60:
        color = 0x00CC66
    elif accuracy >= 45:
        color = 0xFFAA00
    else:
        color = 0xFF4444

    embed = discord.Embed(
        title=f"\U0001f4c8 Forecast Report Card — {ticker} ({date_str})",
        color=color,
    )
    embed.add_field(name="Bot Accuracy", value=f"{correct}/{total} ({accuracy:.0f}%)", inline=True)
    # Historical days may carry a graded 5m companion stream alongside the
    # 15m one — break the headline number down when more than one interval
    # is present in a day's results.
    intervals = sorted({r.get("interval", FORECAST_INTERVAL_MINUTES) for r in results})
    if len(intervals) > 1:
        for iv in intervals:
            iv_scored = [r for r in scored if r.get("interval", FORECAST_INTERVAL_MINUTES) == iv]
            iv_correct = sum(1 for r in iv_scored if r["correct"])
            iv_pct = iv_correct / len(iv_scored) * 100 if iv_scored else 0
            embed.add_field(name=f"{iv}m", value=f"{iv_correct}/{len(iv_scored)} ({iv_pct:.0f}%)", inline=True)
    if user_score and user_score[1] > 0:
        u_correct, u_total = user_score
        embed.add_field(name="Your Calls", value=f"{u_correct}/{u_total} ({u_correct / u_total * 100:.0f}%)", inline=True)

    # The line list sticks to the main interval — a 5m stream would drown it
    # (full breakdown lives behind View Details).
    lines = []
    for r in results:
        if len(intervals) > 1 and r.get("interval", FORECAST_INTERVAL_MINUTES) != max(intervals):
            continue
        ftime = datetime.fromisoformat(r["time"]).astimezone(ET).strftime("%I:%M %p")
        if r.get("correct") is None:
            icon = "\U0001f7e1"
        elif r["correct"]:
            icon = "✅"
        else:
            icon = "❌"
        lines.append(f"{icon} {ftime}: {r['direction']} ({r['confidence']}%) -> {r['actual']} ({r['move']:+.2f})")
    if lines:
        name = f"Forecasts ({max(intervals)}m)" if len(intervals) > 1 else "Forecasts"
        embed.add_field(name=name, value="\n".join(lines[:10]), inline=False)

    embed.set_footer(text=f"Saved to data/results-{date_str}.json | Click View Details for full breakdown")
    return embed


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
