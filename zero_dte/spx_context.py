"""Pure, as-of SPX context v1. Descriptions are not probability or trade signals.

Only completed, received bars are visible. No provider calls or tuning live here.
Five-minute geometry describes observed pressure; it does not predict reversal.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math

from core import market_calendar
from zero_dte.orb_audit import flow_filter, session_time, utc, valid_bar

VERSION = "spx-context-v1"
_MINUTE = timedelta(minutes=1)


def _known(bars, day, now, *, receipts=True):
    """Reject duplicates and malformed rows; do not fill an observation gap."""
    known, duplicates = {}, set()
    for raw in bars:
        try:
            stamp = utc(raw["timestamp"])
            if stamp in known:
                duplicates.add(stamp)
            if not valid_bar(raw, day, now):
                continue
            if receipts:
                available = utc(raw["available_at"])
                if not stamp + _MINUTE <= available <= now:
                    continue
            elif raw.get("available_at") is not None and utc(raw["available_at"]) > now:
                continue
            known[stamp] = {k: float(raw[k]) for k in ("open", "high", "low", "close")}
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
    return {t: b for t, b in known.items() if t not in duplicates}


def _span(known, start, end):
    values = []
    while start < end:
        if start not in known:
            return None
        values.append(known[start])
        start += _MINUTE
    return values


def _previous_levels(previous_bars, day, now):
    previous = day - timedelta(days=1)
    while not market_calendar.is_trading_day(previous):
        previous -= timedelta(days=1)
    bars = _known(previous_bars, previous, now, receipts=False)
    complete = _span(bars, session_time(previous, 9, 30),
                     session_time(previous, *market_calendar.close_time(previous)))
    if not complete:
        return None
    return {"high": max(b["high"] for b in complete), "low": min(b["low"] for b in complete),
            "close": complete[-1]["close"]}


def _part(state, text):
    return {"state": state, "text": text}


def _momentum(known, opening, latest):
    end = latest + _MINUTE
    blocks = int((end - opening).total_seconds() // 300)
    if blocks < 2:
        return _part("unavailable", "Need two complete, non-overlapping 5m candles.")
    boundary = opening + timedelta(minutes=blocks * 5)
    rows = _span(known, boundary - timedelta(minutes=10), boundary)
    if rows is None:
        return _part("unavailable", "Missing minutes prevent a complete 5m momentum comparison.")
    aggregates = [{"open": r[0]["open"], "close": r[-1]["close"],
                   "high": max(b["high"] for b in r), "low": min(b["low"] for b in r)}
                  for r in (rows[:5], rows[5:])]
    before, current = aggregates
    old_move = before["close"] - before["open"]
    move = current["close"] - current["open"]
    higher = current["high"] > before["high"] and current["low"] > before["low"]
    lower = current["high"] < before["high"] and current["low"] < before["low"]
    if higher and move > 0:
        pressure = "strengthening" if old_move <= 0 or move > old_move else "fading" if move < old_move else "steady"
        state = "up_" + pressure
        label = "Higher 5m highs/lows; upward pressure " + pressure
    elif lower and move < 0:
        pressure = "strengthening" if old_move >= 0 or move < old_move else "fading" if move > old_move else "steady"
        state = "down_" + pressure
        label = "Lower 5m highs/lows; downward pressure " + pressure
    else:
        state, label = "mixed", "5m geometry and net movement are mixed; no clear momentum alignment"
    stamp = boundary.astimezone(market_calendar.ET).strftime("%H:%M ET")
    return _part(state, f"{label}. Completed through {stamp}; descriptive, not predictive.")


def _confirmation(direction, flow, proxy, now):
    sources = {}
    texts, statuses = [], []
    checked = flow_filter(flow, now, "UP")
    if checked["status"] == "missing":
        reason = checked.get("reason", "unavailable")
        sources["options_flow"] = {"used": False, "status": reason}
        texts.append("Experimental SPXW flow unavailable/insufficient; not a bearish or bullish vote.")
        statuses.append("flow_missing")
    else:
        ratio = checked["ratio"]
        leaning = "UP" if ratio >= .15 else "DOWN" if ratio <= -.15 else "NEUTRAL"
        state = "mixed" if leaning == "NEUTRAL" else (
            "aligned" if direction == leaning else "opposed" if direction != "NEUTRAL" else "directional")
        sources["options_flow"] = {"used": True, "status": state}
        texts.append(f"Experimental sampled SPXW flow: {leaning.lower()} lean ({state}); does not change price structure.")
        statuses.append("flow_" + state + "_" + leaning.lower())
    proxy_ok = False
    reason = "unavailable"
    if proxy:
        reason = proxy.get("status", "unavailable")
        if reason == "available":
            try:
                end = utc(proxy["bar_at"]) + _MINUTE
                ratio = float(proxy["volume_ratio"])
                # Strict same-session bar and receipt checks: a provider's
                # 'available' flag alone does not make a stale proxy current.
                proxy_ok = (end.astimezone(market_calendar.ET).date() == now.astimezone(market_calendar.ET).date()
                            and 0 <= (now - end).total_seconds() <= 90
                            and type(proxy["above_vwap"]) is bool and math.isfinite(ratio) and ratio >= 0)
                if proxy.get("received_at") is not None:
                    proxy_ok = proxy_ok and end <= utc(proxy["received_at"]) <= now
                reason = "stale_or_invalid" if not proxy_ok else "available"
            except (KeyError, ValueError, TypeError, OverflowError):
                reason = "stale_or_invalid"
    if proxy_ok:
        side = "UP" if proxy["above_vwap"] else "DOWN"
        state = "aligned" if direction == side else "opposed" if direction != "NEUTRAL" else "directional"
        sources["spy_proxy"] = {"used": True, "status": state}
        texts.append(f"SPY proxy (Yahoo indicative): {'above' if proxy['above_vwap'] else 'at/below'} its VWAP; "
                     f"last completed 5m volume {float(proxy['volume_ratio']):.2f}x the preceding three 5m mean ({state}). "
                     "SPY volume is not SPX volume; real-time completeness is not guaranteed.")
        statuses.append("proxy_" + state + "_" + side.lower())
    else:
        # Never echo arbitrary provider/exception strings into Discord or state.
        safe = reason if reason in {"unavailable", "stale_or_invalid", "missing", "disabled", "failed", "insufficient"} else "unavailable"
        sources["spy_proxy"] = {"used": False, "status": safe}
        texts.append("SPY volume/VWAP proxy unavailable; no volume confirmation assumed.")
        statuses.append("proxy_missing")
    return _part("_".join(statuses), "\n".join(texts)), sources


def build_context(record: dict, previous_bars: list, now: datetime,
                  flow: dict | None = None, proxy: dict | None = None) -> dict:
    """Build an immutable JSON-compatible snapshot from evidence visible at now.

    Direction means current opening-range location, not the next bar's forecast.
    A complete session-to-latest sequence is required to name an ORB episode.
    """
    now = utc(now)
    day = date.fromisoformat(record["session_date"])
    opening, range_end = session_time(day, 9, 30), session_time(day, 9, 45)
    close = session_time(day, *market_calendar.close_time(day))
    known = _known(record.get("bars", []), day, now)
    latest = max(known, default=None)
    fresh = (latest is not None and market_calendar.is_trading_day(day)
             and opening <= now <= close and 0 <= (now - latest - _MINUTE).total_seconds() <= 90)
    out = {"schema": 1, "version": VERSION, "session_date": day.isoformat(), "as_of": now.isoformat(),
           "bar_at": latest.isoformat() if latest else None, "fresh": fresh, "direction": "NEUTRAL",
           "sources": {"spx": {"used": bool(fresh), "status": "fresh" if fresh else "stale_or_missing"},
                       "prior_spx": {"used": False, "status": "unavailable"}}}
    if not fresh:
        out["structure"] = _part("unavailable", "SPX completed-minute data stale, missing, or session closed; no current directional read.")
        out["setup"] = _part("indeterminate", "Setup unavailable; do not interpret missing data as consolidation.")
        out["momentum"] = _part("unavailable", "Momentum unavailable without fresh SPX observations.")
        out["invalidation"] = _part("unavailable", "No valid current interpretation to invalidate; wait for fresh data.")
    else:
        contiguous = _span(known, opening, latest + _MINUTE)
        range_bars = _span(known, opening, range_end) if now >= range_end else None
        if now < range_end:
            out["structure"] = _part("forming", "09:30–09:45 ET opening range is still forming.")
            out["setup"] = _part("waiting", "Wait for the full opening range; no completed ORB yet.")
            out["invalidation"] = _part("waiting", "No breakout interpretation until all 15 opening minutes complete.")
        elif range_bars is None or contiguous is None:
            out["structure"] = _part("incomplete", "SPX is fresh but missing session minutes prevent a reliable opening-range episode.")
            out["setup"] = _part("indeterminate", "Cannot establish the breakout/retest sequence across a data gap.")
            out["invalidation"] = _part("unavailable", "No directional setup to invalidate until coverage recovers.")
        else:
            high, low = max(b["high"] for b in range_bars), min(b["low"] for b in range_bars)
            last = known[latest]
            def side(bar):
                return "UP" if bar["close"] > high else "DOWN" if bar["close"] < low else "NEUTRAL"
            direction = side(last) if latest >= range_end else "NEUTRAL"
            out["direction"] = direction
            location = {"UP": "Above", "DOWN": "Below", "NEUTRAL": "Inside"}[direction]
            session_high = max(b["high"] for b in contiguous)
            session_low = min(b["low"] for b in contiguous)
            out["structure"] = _part({"UP": "above_range", "DOWN": "below_range", "NEUTRAL": "inside_range"}[direction],
                f"{location} the 15m opening range {low:.2f}–{high:.2f}. Observed session extremes {session_low:.2f}–{session_high:.2f}.")
            post_range = [known[t] for t in sorted(known) if range_end <= t < latest]
            prior_side = side(known[latest - _MINUTE]) if latest - _MINUTE in known and latest > range_end else "NEUTRAL"
            if direction == "NEUTRAL":
                if prior_side != "NEUTRAL":
                    out["setup"] = _part("failed_" + prior_side.lower(), f"{'Upside' if prior_side == 'UP' else 'Downside'} attempt failed: completed close returned inside the opening range.")
                else:
                    out["setup"] = _part("inside_range", "Inside the opening range; no directional breakout is established.")
                out["invalidation"] = _part("range_break", f"Range-bound interpretation changes on a completed close above {high:.2f} or below {low:.2f}; reassess follow-through.")
            else:
                suffix = direction.lower()
                retest = prior_side == direction and (last["low"] <= high if direction == "UP" else last["high"] >= low)
                if retest:
                    state, text = "retest_" + suffix, "Range boundary revisited and close remained outside; observed retest, not an entry recommendation."
                elif prior_side == direction:
                    state, text = "holding_" + suffix, "Consecutive closes remain outside the opening range; breakout holding so far."
                elif any(side(b) == direction for b in post_range):
                    state, text = "reclaimed_" + suffix, "Price closed back outside a previously attempted range boundary; renewed attempt, not the first breakout."
                else:
                    state, text = "breakout_" + suffix, "First completed close through this opening-range boundary; follow-through not yet established."
                out["setup"] = _part(state, text)
                level = high if direction == "UP" else low
                out["invalidation"] = _part("back_inside_" + suffix,
                    f"{'Upside' if direction == 'UP' else 'Downside'} holding interpretation fails on a completed close {'at/below' if direction == 'UP' else 'at/above'} {level:.2f}. This is scenario invalidation, not an option stop.")
        out["momentum"] = _momentum(known, opening, latest)
        levels = _previous_levels(previous_bars, day, now)
        if levels is not None:
            out["sources"]["prior_spx"] = {"used": True, "status": "complete_previous_session"}
            out["structure"]["text"] += f" Prior-session high/low/close: {levels['high']:.2f} / {levels['low']:.2f} / {levels['close']:.2f}. Reference levels, not a daily bias."
        else:
            out["structure"]["text"] += " Complete immediately prior-session levels unavailable."
    out["confirmation"], confirmation_sources = _confirmation(out["direction"], flow, proxy, now)
    out["sources"].update(confirmation_sources)
    from zero_dte.spx_actions import build_action
    out['action'] = build_action(out)
    # Invalidation MUST remain last: runtime uses signature[-1] for urgency.
    out["signature"] = [out[k]["state"] for k in ("structure", "setup", "momentum", "confirmation", "action", "invalidation")]
    return out


def grade_context(assessment: dict, bars: list, now: datetime) -> dict:
    """Paper forward SPX movement only; caller must retain finalized outcomes.

    Entry is the next minute open strictly AFTER as_of. Each horizon requires
    every held minute; a late backfill must not overwrite a finalized missing
    outcome. Neutral descriptions record movement but receive no signed score.
    """
    now = utc(now)
    available = utc(assessment["as_of"])
    day = date.fromisoformat(assessment["session_date"])
    if available.astimezone(market_calendar.ET).date() != day:
        raise ValueError("Assessment date differs from availability")
    direction = assessment["direction"]
    if direction not in {"UP", "DOWN", "NEUTRAL"}:
        raise ValueError("Invalid context direction")
    entry = available.replace(second=0, microsecond=0) + _MINUTE
    close = session_time(day, *market_calendar.close_time(day))
    known = _known(bars, day, now)
    outcomes = {}
    for minutes in (15, 30):
        exit_time = entry + timedelta(minutes=minutes)
        base = {"direction": direction, "entry_time": entry.isoformat(), "exit_time": exit_time.isoformat(),
                "horizon_minutes": minutes, "points": None, "signed_points": None}
        if now < available:
            outcomes[str(minutes)] = None
        elif not assessment.get("fresh", False):
            outcomes[str(minutes)] = {**base, "status": "invalid_entry", "reason": "noncurrent_assessment"}
        elif entry < session_time(day, 9, 30) or exit_time > close:
            outcomes[str(minutes)] = {**base, "status": "session_ended", "reason": "horizon_crosses_close"}
        elif now < exit_time:
            outcomes[str(minutes)] = None
        else:
            held = _span(known, entry, exit_time)
            if held is None:
                outcomes[str(minutes)] = (None if now <= exit_time + timedelta(seconds=90) else
                    {**base, "status": "missing", "reason": "missing_held_minutes"})
            else:
                points = held[-1]["close"] - held[0]["open"]
                signed = points if direction == "UP" else -points if direction == "DOWN" else None
                outcomes[str(minutes)] = {**base, "status": "complete", "entry_price": held[0]["open"],
                    "exit_price": held[-1]["close"], "points": points, "signed_points": signed}
    return outcomes
