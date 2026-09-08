"""Frozen SPX ORB v1 paper experiment. No network, orders, or option P&L.

Bars are minute-start stamped. First-seen complete bars are immutable. Receipts
are observation times, not proof the data was available earlier. Outcomes use
historical OHLC only AFTER the recorded decision time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math

from core import market_calendar

ET = market_calendar.ET
UTC = timezone.utc
RULE = "spx-orb-15m-first-break-v1"


def utc(value: datetime | str) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(UTC)


def session_time(day: date, hour: int, minute: int = 0) -> datetime:
    return ET.localize(datetime(day.year, day.month, day.day, hour, minute)).astimezone(UTC)


def new_record(day: date) -> dict:
    return {"schema": 1, "rule": RULE, "paper_only": True,
            "session_date": day.isoformat(), "bars": [], "candidate": None,
            "outcome": None, "status": "waiting", "polls": 0,
            "fetch_failures": 0, "rejected_bars": 0}


def validate_record(record: dict, day: date) -> None:
    """Reject incompatible/corrupt files rather than silently restarting a day."""
    if (record.get("schema") != 1 or record.get("rule") != RULE
            or record.get("session_date") != day.isoformat()
            or record.get("paper_only") is not True or not isinstance(record.get("bars"), list)):
        raise ValueError("Invalid audit record")
    seen = set()
    for bar in record["bars"]:
        stamp = utc(bar["timestamp"])
        available = utc(bar["available_at"])
        if stamp in seen or available < stamp + timedelta(minutes=1):
            raise ValueError("Invalid persisted bar")
        if not valid_bar(bar, day, available):
            raise ValueError("Invalid persisted bar")
        seen.add(stamp)
    candidate = record.get("candidate")
    if candidate is not None:
        if not isinstance(candidate, dict) or candidate.get("status") not in {"prospective", "missed"}:
            raise ValueError("Invalid persisted candidate")
        utc(candidate["available_at"])
        utc(candidate["signal_bar"])
        if candidate["direction"] not in {"UP", "DOWN"}:
            raise ValueError("Invalid persisted direction")
        if candidate["status"] == "prospective":
            utc(candidate["entry_time"])
    if record.get("outcome") is not None and not isinstance(record["outcome"], dict):
        raise ValueError("Invalid persisted outcome")


def valid_bar(bar: dict, day: date, received_at: datetime) -> bool:
    try:
        stamp = utc(bar["timestamp"])
        values = [float(bar[k]) for k in ("open", "high", "low", "close")]
        o, h, l, c = values
        end = session_time(day, *market_calendar.close_time(day))
        return (market_calendar.is_trading_day(day)
                and stamp.second == 0 and stamp.microsecond == 0
                and session_time(day, 9, 30) <= stamp < end
                and stamp + timedelta(minutes=1) <= utc(received_at)
                and all(math.isfinite(v) and v > 0 for v in values)
                and l <= min(o, c) <= max(o, c) <= h)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def flow_filter(flow: dict | None, available_at: datetime, direction: str) -> dict:
    """Missing/stale/unclassifiable data is not a negative filter observation."""
    result = {"status": "missing", "reason": "no_cached_flow"}
    if not flow:
        return result
    try:
        age = (utc(available_at) - utc(flow["sample_at"])).total_seconds()
        captured = utc(flow["captured_at"])
        if not 0 <= age <= 120 or not utc(flow["sample_at"]) <= captured <= utc(available_at):
            return {"status": "missing", "reason": "stale_or_future_flow"}
        if flow.get("latest_trade_at") is None:
            return {"status": "missing", "reason": "source_time_unavailable"}
        trade_age = (utc(available_at) - utc(flow["latest_trade_at"])).total_seconds()
        if not 0 <= trade_age <= 120:
            return {"status": "missing", "reason": "stale_or_future_trade_data",
                    "latest_trade_age_seconds": trade_age}
        total, classified = int(flow["total_trades"]), int(flow["classified_trades"])
        ratio = float(flow["ratio"])
        if total <= 0 or not 0 <= classified <= total or not math.isfinite(ratio) or abs(ratio) > 1:
            return {"status": "missing", "reason": "invalid_flow"}
        pct = classified / total * 100
        base = {"sample_at": utc(flow["sample_at"]).isoformat(), "age_seconds": age,
                "latest_trade_at": utc(flow["latest_trade_at"]).isoformat(),
                "latest_trade_age_seconds": trade_age, "new_trade_rows": flow.get("new_trade_rows"),
                "classified_trades": classified, "classified_pct": pct, "ratio": ratio}
        if classified < 25 or pct < 20:
            return {**base, "status": "missing", "reason": "insufficient_classifiable_flow"}
        aligned = ratio if direction == "UP" else -ratio
        return {**base, "status": "qualifying" if aligned >= .15 else "filtered",
                "reason": "aligned_flow" if aligned >= .15 else "flow_not_aligned"}
    except (KeyError, ValueError, TypeError, AttributeError):
        return {"status": "missing", "reason": "invalid_flow"}


def update_record(record: dict, bars, received_at: datetime, flow: dict | None = None,
                  *, data_cutoff: datetime | None = None) -> dict:
    """Mutate one session record using a response received now; no replay fiction.

    `bars` accepts IndexBar instances or normalized dictionaries. Future and
    incomplete bars are dropped, not cached for later use. `data_cutoff` is the
    requested coverage boundary, distinct from receipt time: response latency
    cannot make a partial requested bar complete. Duplicate timestamps within a
    response are rejected. Existing first-seen observations never change.
    """
    received_at = utc(received_at)
    coverage = min(received_at, utc(data_cutoff)) if data_cutoff is not None else received_at
    day = date.fromisoformat(record["session_date"])
    if received_at.astimezone(ET).date() != day:
        raise ValueError("Receipt must belong to record session")
    validate_record(record, day)
    known = {utc(b["timestamp"]): b for b in record["bars"]}
    incoming, duplicates = {}, set()
    for bar in bars:
        try:
            row = dict(bar) if isinstance(bar, dict) else {
                k: getattr(bar, k) for k in ("timestamp", "open", "high", "low", "close")}
            if not valid_bar(row, day, coverage):
                raise ValueError("Invalid bar")
            stamp = utc(row["timestamp"])
            if stamp in incoming:
                duplicates.add(stamp)
            incoming[stamp] = {"timestamp": stamp.isoformat(), "available_at": received_at.isoformat(),
                               **{k: float(row[k]) for k in ("open", "high", "low", "close")}}
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            record["rejected_bars"] += 1
    record["rejected_bars"] += len(duplicates)
    for stamp, row in incoming.items():
        if stamp not in duplicates:
            known.setdefault(stamp, row)
    record["bars"] = [known[t] for t in sorted(known)]
    record["last_success_at"] = received_at.isoformat()
    if record["candidate"] is None:
        _find_candidate(record, known, received_at, flow)
    candidate = record["candidate"]
    if candidate and candidate["status"] == "prospective" and record["outcome"] is None:
        record["outcome"] = evaluate_outcome(candidate, known, received_at)
        record["status"] = "outcome_pending" if record["outcome"] is None else record["outcome"]["status"]
    return record


def _find_candidate(record, known, now, flow):
    day = date.fromisoformat(record["session_date"])
    opening = session_time(day, 9, 30)
    opening_bars = [known.get(opening + timedelta(minutes=i)) for i in range(15)]
    deadline = session_time(day, 11)
    if not all(opening_bars):
        record["status"] = "missing_opening_range" if now >= deadline else "waiting_opening_range"
        return
    high = max(b["high"] for b in opening_bars)
    low = min(b["low"] for b in opening_bars)
    record["opening_range"] = {"high": high, "low": low}
    if high <= low:
        record["status"] = "invalid_opening_range"
        return
    stamp = opening + timedelta(minutes=15)
    # Decision closes strictly before 11:00 ET (last eligible bar 10:58).
    while stamp + timedelta(minutes=1) < deadline and stamp + timedelta(minutes=1) <= now:
        bar = known.get(stamp)
        if bar is None:
            record["status"] = "missing_pre_signal_bar"
            return  # Cannot assert this is the FIRST breakout across a gap.
        if bar["close"] > high or bar["close"] < low:
            direction = "UP" if bar["close"] > high else "DOWN"
            age = (now - stamp - timedelta(minutes=1)).total_seconds()
            prospective = age <= 90 and now < deadline
            entry_time = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            record["candidate"] = {
                "status": "prospective" if prospective else "missed",
                "reason": "timely_first_break" if prospective else "late_first_break",
                "signal_bar": stamp.isoformat(), "signal_close": bar["close"],
                "available_at": now.isoformat(), "signal_age_seconds": age,
                "direction": direction, "range_high": high, "range_low": low,
                "entry_time": entry_time.isoformat() if prospective else None,
                "flow": flow_filter(flow, now, direction) if prospective else {
                    "status": "missing", "reason": "missed_price_setup"},
            }
            record["status"] = "outcome_pending" if prospective else "missed"
            return
        stamp += timedelta(minutes=1)
    record["status"] = "no_breakout" if now >= deadline else "waiting_breakout"


def evaluate_outcome(candidate: dict, known: dict, now: datetime) -> dict | None:
    """Conservative OHLC paper outcome; None means not yet observable.

    Excursions include the full exit bar and are upper bounds, not a reconstructed
    intrabar path. A missing minute is terminal only after the 30m horizon plus
    a 90s collection grace; it is never forward-filled.
    """
    now = utc(now)
    entry_time = utc(candidate["entry_time"])
    if entry_time <= utc(candidate["available_at"]):
        raise ValueError("Entry must follow decision availability")
    end = entry_time + timedelta(minutes=30)
    def missing(reason):
        return {"status": "missing", "reason": reason,
                "observed_at": now.isoformat()} if now >= end + timedelta(seconds=90) else None
    entry_bar = known.get(entry_time)
    if entry_bar is None:
        return missing("missing_entry_bar")
    entry = entry_bar["open"]
    sign = 1 if candidate["direction"] == "UP" else -1
    stop = candidate["range_low"] if sign == 1 else candidate["range_high"]
    risk = sign * (entry - stop)
    if risk <= 0:
        return {"status": "invalid_entry", "reason": "open_at_or_beyond_stop",
                "observed_at": now.isoformat()}
    target = entry + sign * risk
    mfe = mae = 0.0
    for i in range(30):
        stamp = entry_time + timedelta(minutes=i)
        if stamp + timedelta(minutes=1) > now:
            return None
        bar = known.get(stamp)
        if bar is None:
            return missing("missing_outcome_bar")
        favorable = bar["high"] if sign == 1 else bar["low"]
        adverse = bar["low"] if sign == 1 else bar["high"]
        mfe = max(mfe, sign * (favorable - entry))
        mae = max(mae, -sign * (adverse - entry))
        stop_hit = sign * (adverse - stop) <= 0
        target_hit = sign * (favorable - target) >= 0
        ambiguous = False
        if sign * (bar["open"] - stop) <= 0:
            exit_price, reason = bar["open"], "gap_stop"
        elif sign * (bar["open"] - target) >= 0:
            exit_price, reason = target, "gap_target_capped"
        elif stop_hit:
            exit_price, reason = stop, "stop"
            ambiguous = target_hit
        elif target_hit:
            exit_price, reason = target, "target"
        elif i == 29:
            exit_price, reason = bar["close"], "time_exit"
        else:
            continue
        points = sign * (exit_price - entry)
        return {"status": "complete", "reason": reason, "entry_time": entry_time.isoformat(),
                "entry_spx": entry, "stop_spx": stop, "target_spx": target,
                "risk_points": risk, "exit_bar": stamp.isoformat(), "exit_spx": exit_price,
                "observed_at": now.isoformat(), "signed_points": points, "r_multiple": points / risk,
                "ambiguous_stop_target": ambiguous,
                "mfe_points_upper_bound": mfe, "mae_points_upper_bound": mae,
                "excursion_basis": "full_minute_bars_including_exit_bar",
                "not_option_pnl": True}
    return None
