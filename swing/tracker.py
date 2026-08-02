"""Swing signal persistence and outcome grading.

Data kinds (all additive to the existing data/ history):
  swing_signals-YYYY-MM-DD.jsonl  — immutable append at signal time
  swing_checks-YYYY-MM-DD.jsonl   — midday confirmation/invalidation events
  swing_outcomes-YYYY-MM-DD.jsonl — graded results (date = grading date)
  swing_open.json                 — registry of currently-open signals

The registry is what the midday check and the grader iterate — it self-heals:
signals that matured while the bot was offline are graded at the next pre-open
pass because grading scans the registry, not the calendar.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta

import pandas as pd
import pytz

from core.paths import SWING_OPEN_FILE
from core.tracking import append_jsonl, load_json, write_atomic
from swing.options import requote
from swing.strategy import SwingSignal

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# Rough delta of the near-ATM entry, for the fallback option P&L estimate.
FALLBACK_DELTA = 0.5
# A move smaller than this (%) grades no indicator either way.
INDICATOR_DEADBAND_PCT = 0.5


def signal_to_dict(sig: SwingSignal) -> dict:
    d = {
        "signal_id": sig.signal_id,
        "ticker": sig.ticker,
        "direction": sig.direction,
        "entry_price": sig.vote.close,
        "invalidation": sig.invalidation,
        "entry_zone_low": sig.entry_zone_low,
        "entry_zone_high": sig.entry_zone_high,
        "no_chase": sig.no_chase,
        "target_pct": sig.target_pct,
        "horizon_days": sig.horizon_days,
        "signal_date": sig.signal_date,
        "created_at": sig.created_at,
        "score": sig.vote.score,
        "confidence": sig.vote.confidence,
        "signals": sig.vote.signals,
        "weights": sig.vote.weights,
        "atr14": sig.vote.atr14,
        "rsi_d": sig.vote.rsi_d,
        "status": "OPEN",
    }
    d["contract"] = asdict(sig.contract) if sig.contract is not None else None
    return d


# ── Open-signal registry ─────────────────────────────────────────────────

def load_open_signals() -> list[dict]:
    return load_json(SWING_OPEN_FILE, default=[]) or []


def _save_open_signals(entries: list[dict]) -> None:
    os.makedirs(os.path.dirname(SWING_OPEN_FILE), exist_ok=True)
    write_atomic(SWING_OPEN_FILE, json.dumps(entries, indent=2))


def log_swing_signal(sig: SwingSignal) -> dict:
    """Append to today's swing_signals jsonl and register as open."""
    entry = signal_to_dict(sig)
    append_jsonl("swing_signals", sig.signal_date, entry)
    open_sigs = [s for s in load_open_signals() if s["signal_id"] != entry["signal_id"]]
    open_sigs.append(entry)
    _save_open_signals(open_sigs)
    return entry


def set_signal_status(signal_id: str, status: str) -> None:
    """Update a registry entry's status (OPEN / TARGET / INVALIDATED)."""
    open_sigs = load_open_signals()
    for s in open_sigs:
        if s["signal_id"] == signal_id:
            s["status"] = status
    _save_open_signals(open_sigs)


def close_signal(signal_id: str) -> None:
    _save_open_signals([s for s in load_open_signals() if s["signal_id"] != signal_id])


def log_swing_check(entry: dict, now_et: datetime) -> None:
    append_jsonl("swing_checks", now_et.strftime("%Y-%m-%d"), entry)


def last_signal_dates(lookback_days: int = 30) -> dict[tuple[str, str], str]:
    """(ticker, direction) -> most recent signal date, for the cooldown check."""
    from core.tracking import available_dates, load_jsonl
    seen: dict[tuple[str, str], str] = {}
    for d in available_dates("swing_signals")[-lookback_days:]:
        for e in load_jsonl("swing_signals", d):
            key = (e["ticker"], e["direction"])
            if key not in seen or e["signal_date"] > seen[key]:
                seen[key] = e["signal_date"]
    return seen


# ── Grading ──────────────────────────────────────────────────────────────

def _horizon_end(sig: dict) -> datetime.date:
    start = datetime.strptime(sig["signal_date"], "%Y-%m-%d").date()
    end = start + timedelta(days=sig["horizon_days"])
    contract = sig.get("contract")
    if contract and contract.get("expiry"):
        try:
            expiry = datetime.strptime(contract["expiry"], "%Y-%m-%d").date()
            return min(end, expiry)
        except ValueError:
            pass
    return end


def _walk_forward(sig: dict, days: pd.DataFrame,
                  asof=None) -> tuple[str, str, float] | None:
    """Replay daily candles from the signal date: did the underlying hit the
    target or the invalidation first? Same-day both -> stop wins (conservative).

    `asof` (a date) lets the caller assert the horizon has already passed even
    when no candle dated past it exists — a horizon landing on a holiday or
    weekend otherwise never grades and the signal blocks its ticker forever.

    Returns (resolution, resolution_date, move_pct) or None if not resolvable yet.
    """
    entry = float(sig["entry_price"])
    target_move = entry * float(sig["target_pct"]) / 100.0
    is_call = sig["direction"] == "CALL"
    horizon_end = _horizon_end(sig)

    resolved = None
    last_close = None
    last_date = None
    passed_horizon = asof is not None and asof > horizon_end
    for _, row in days.iterrows():
        d = row["date"]
        if d > horizon_end:
            passed_horizon = True
            break
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        last_close, last_date = close, d
        if is_call:
            if low <= float(sig["invalidation"]):
                resolved = ("INVALIDATED", d)
                break
            if high >= entry + target_move:
                resolved = ("TARGET_HIT", d)
                break
        else:
            if high >= float(sig["invalidation"]):
                resolved = ("INVALIDATED", d)
                break
            if low <= entry - target_move:
                resolved = ("TARGET_HIT", d)
                break

    if resolved:
        res, d = resolved
        move_pct = round((float(sig["invalidation"]) - entry) / entry * 100, 2) if res == "INVALIDATED" \
            else round((sig["target_pct"] if is_call else -sig["target_pct"]), 2)
        return res, d.strftime("%Y-%m-%d"), move_pct

    horizon_reached = passed_horizon or (last_date is not None and last_date >= horizon_end)
    if horizon_reached and last_close is not None:
        move_pct = round((last_close - entry) / entry * 100, 2)
        favorable = move_pct > 0 if is_call else move_pct < 0
        return ("EXPIRED_WIN" if favorable else "EXPIRED_LOSS"), last_date.strftime("%Y-%m-%d"), move_pct

    return None  # horizon not reached yet (or no candles inside it — widen the fetch)


async def _option_pnl(sig: dict, move_pct: float, resolution_date, today) -> dict | None:
    """Option P&L as of the RESOLUTION date — the day the exit signal fired.

    A live requote prices the contract *now*, which is only the exit price when
    grading runs the same day the signal resolved. Grading is a pre-open pass,
    so a signal that resolves mid-horizon is normally requoted 1+ days later —
    and on a 7-14 DTE contract those extra days of theta are most of what is
    left. Requoting late made winners look like losses (a TARGET_HIT graded 3
    days on logged -99.1%), always biasing P&L down, since theta only ever runs
    against a long holder.

    So: use the live quote only when it is contemporaneous with the exit, and
    otherwise fall back to the delta model, which prices off the resolution-day
    move and carries no timing error. Either way record how stale the pricing
    is (`quote_lag_days`, `priced_asof`) so the ledger stays auditable — the
    fallback ignores theta and reads optimistic, the opposite bias.
    """
    contract = sig.get("contract")
    if not contract or not contract.get("mid"):
        return None
    entry_mid = float(contract["mid"])
    try:
        expiry = datetime.strptime(contract["expiry"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        expiry = None

    lag_days = (today - resolution_date).days
    meta = {"priced_asof": resolution_date.strftime("%Y-%m-%d"), "quote_lag_days": lag_days}

    if lag_days <= 0 and expiry and today <= expiry:
        quote = await asyncio.to_thread(
            requote, sig["ticker"], contract["expiry"], float(contract["strike"]), contract["kind"])
        if quote is not None:
            return {"entry_mid": entry_mid, "exit_quote": quote,
                    "pnl_pct": round((quote - entry_mid) / entry_mid * 100, 1),
                    "pnl_method": "requote", **meta}

    # Fallback: delta-approximate from the underlying move at resolution.
    move_dollars = float(sig["entry_price"]) * move_pct / 100.0
    signed = move_dollars if sig["direction"] == "CALL" else -move_dollars
    est = signed * FALLBACK_DELTA
    return {"entry_mid": entry_mid, "exit_quote": None,
            "pnl_pct": round(est / entry_mid * 100, 1),
            "pnl_method": "delta_est", **meta}


async def grade_due_signals(client, now_et: datetime) -> list[dict]:
    """Grade every open signal that has resolved or reached its horizon.

    Runs during the pre-open pass (yesterday's candle is complete). Fetches
    daily candles per due ticker (1 call each; usually 0-2 signals are due).
    Returns the outcome dicts it appended to swing_outcomes.
    """
    today = now_et.date()
    outcomes = []
    for sig in load_open_signals():
        due = today > _horizon_end(sig) or sig.get("status") in ("TARGET", "INVALIDATED")
        # Also try grading anything whose horizon end is today or earlier —
        # the walk decides whether it actually resolved.
        if not due and today < _horizon_end(sig):
            continue
        try:
            # 90 bars ≈ 4 months: keeps signals gradable even after a long
            # outage (a 40-bar window couldn't reach back past ~8 weeks).
            df = await client.fetch_candles(sig["ticker"], interval="D", count=90)
        except Exception as exc:
            logger.warning("[%s] Grading fetch failed: %s", sig["ticker"], exc)
            continue
        if df is None or df.empty:
            continue

        timestamps = pd.to_datetime(df["timestamp"])
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize("UTC")
        days = df.copy()
        days["date"] = timestamps.dt.tz_convert(ET).dt.date
        signal_date = datetime.strptime(sig["signal_date"], "%Y-%m-%d").date()
        days = days[(days["date"] >= signal_date) & (days["date"] < today)]
        if days.empty:
            continue

        result = _walk_forward(sig, days, asof=today)
        if result is None:
            continue
        resolution, resolution_date, move_pct = result

        resolved_on = datetime.strptime(resolution_date, "%Y-%m-%d").date()
        option = await _option_pnl(sig, move_pct, resolved_on, today)

        actual = None
        if move_pct >= INDICATOR_DEADBAND_PCT:
            actual = "BULLISH"
        elif move_pct <= -INDICATOR_DEADBAND_PCT:
            actual = "BEARISH"

        outcome = {
            "signal_id": sig["signal_id"],
            "ticker": sig["ticker"],
            "direction": sig["direction"],
            "entry_price": sig["entry_price"],
            "invalidation": sig["invalidation"],
            "target_pct": sig["target_pct"],
            "signal_date": sig["signal_date"],
            "confidence": sig["confidence"],
            "score": sig["score"],
            "resolution": resolution,
            "resolution_date": resolution_date,
            "graded_at": now_et.astimezone(pytz.UTC).isoformat(),
            "move_pct": move_pct,
            "win": resolution in ("TARGET_HIT", "EXPIRED_WIN"),
            "option": option,
            "signals": sig.get("signals", {}),
            "actual": actual,
        }
        append_jsonl("swing_outcomes", now_et.strftime("%Y-%m-%d"), outcome)
        close_signal(sig["signal_id"])
        outcomes.append(outcome)
        logger.info("[%s] Swing graded: %s %s -> %s (%.2f%%) opt=%s",
                    sig["ticker"], sig["direction"], sig["signal_date"],
                    resolution, move_pct, (option or {}).get("pnl_pct"))
    return outcomes
