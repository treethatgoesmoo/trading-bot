"""Read-only ThetaData audit report; no SDK calls or changes to saved evidence.

--as-of DATE means the end of that Eastern calendar date (inclusive). Without
it, the cutoff is the current instant; today's coverage can therefore be partial.
Request cohorts are selected by Eastern START date, including later completions
known by the cutoff. ORB bars must have been received by the cutoff. Session
counters are latest snapshots, not event-sourced: future snapshots are withheld.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path

from core import market_calendar
from research.core.context_audit_report import context_report
from core.paths import THETADATA_DATA_DIR
from core.theta_usage import _ENDPOINTS, PURPOSES
from research.zero_dte.orb_audit import evaluate_outcome, session_time, utc, validate_record

ET = market_calendar.ET
UTC = timezone.utc
STATUSES = frozenset({
    "waiting", "waiting_opening_range", "missing_opening_range", "invalid_opening_range",
    "missing_pre_signal_bar", "waiting_breakout", "no_breakout", "missed",
    "outcome_pending", "complete", "missing", "invalid_entry", "as_of_status_unavailable",
})


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    return utc(value)


def _days(start, end):
    for i in range((end - start).days):
        yield start + timedelta(days=i)


def _percent(n, d):
    return f"{100 * n / d:.1f}%" if d else "n/a"


def _percentile(values, fraction):
    # Nearest-rank, deliberately no interpolation that hides a slow tail.
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _duration(value):
    return f"{value:.1f}" if value is not None else "n/a"


def _usage_event(row):
    """Project onto validated metadata only; never render arbitrary file strings."""
    if not isinstance(row, dict) or row.get("schema") != 1:
        raise ValueError("Unknown schema")
    event, endpoint = row.get("event"), row.get("endpoint")
    if event not in {"start", "finish"} or endpoint not in _ENDPOINTS:
        raise ValueError("Unknown event or endpoint")
    if row.get("product") != _ENDPOINTS[endpoint]:
        raise ValueError("Wrong product")
    request_id = row.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Invalid identity")
    purpose = row.get("purpose")
    result = {"event": event, "request_id": request_id, "endpoint": endpoint,
              "product": _ENDPOINTS[endpoint],
              "purpose": purpose if isinstance(purpose, str) and purpose in PURPOSES else "unspecified",
              "started_at": _stamp(row.get("started_at"))}
    if event == "finish":
        finished = _stamp(row.get("finished_at"))
        elapsed, success, rows = row.get("elapsed_ms"), row.get("success"), row.get("rows")
        if (finished < result["started_at"] or not _number(elapsed) or elapsed < 0
                or not isinstance(success, bool) or (rows is not None and not _integer(rows))):
            raise ValueError("Invalid completion")
        result.update(finished_at=finished, elapsed_ms=elapsed, success=success, rows=rows)
    return result


def _load_usage(directory, start, end, as_of):
    events, diagnostics = {}, Counter()
    for day in _days(start, min(end, as_of.astimezone(ET).date() + timedelta(days=1))):
        path = directory / f"usage-{day.isoformat()}.jsonl"
        if not path.exists():
            continue
        diagnostics["files"] += 1
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = _usage_event(json.loads(line))
                        cohort = row["started_at"].astimezone(ET).date()
                        if not start <= cohort < end:
                            diagnostics["outside_window"] += 1
                            continue
                        if cohort != day:
                            raise ValueError("Wrong date partition")
                        if row.get("finished_at", row["started_at"]) > as_of:
                            diagnostics["after_cutoff"] += 1
                            continue
                        key = row["request_id"], row["event"]
                        if key in events:
                            diagnostics["duplicates" if events[key] == row else "conflicts"] += 1
                            continue  # First validated event wins; conflict is disclosed.
                        events[key] = row
                    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                        diagnostics["malformed"] += 1
        except (OSError, UnicodeError):
            diagnostics["unreadable_files"] += 1
    requests = defaultdict(dict)
    for (identity, event), row in events.items():
        requests[identity][event] = row
    usable = []
    for pair in requests.values():
        begin, finish = pair.get("start"), pair.get("finish")
        if begin and finish and any(begin[k] != finish[k] for k in (
                "started_at", "endpoint", "product", "purpose")):
            diagnostics["inconsistent_pairs"] += 1
            finish = None  # Do not join a completion to a different request.
        usable.append((begin, finish))
    return usable, diagnostics


def _usage_stats(pairs):
    starts = sum(a is not None for a, _ in pairs)
    finishes = [b for _, b in pairs if b is not None]
    good = [b for b in finishes if b["success"]]
    latency = [b["elapsed_ms"] for b in finishes]
    return (f"attempts={starts} completions={len(finishes)} failures={len(finishes)-len(good)} "
            f"failure_rate={_percent(len(finishes)-len(good), len(finishes))} "
            f"empty={sum(b['rows'] == 0 for b in good)} "
            f"unknown_rows={sum(b['rows'] is None for b in good)} "
            f"raw_rows={sum(b['rows'] or 0 for b in good)} "
            f"p50_ms={_duration(_percentile(latency, .50))} "
            f"p95_ms={_duration(_percentile(latency, .95))} "
            f"unmatched_starts={sum(a is not None and b is None for a, b in pairs)} "
            f"orphan_finishes={sum(a is None and b is not None for a, b in pairs)}")


def _expected_minutes(day, as_of):
    opening = session_time(day, 9, 30)
    close = session_time(day, *market_calendar.close_time(day))
    return max(0, int((min(close, as_of) - opening).total_seconds() // 60))


def _complete_outcome(outcome, candidate):
    for key in ("signed_points", "r_multiple", "risk_points", "entry_spx", "exit_spx"):
        if not _number(outcome.get(key)):
            raise ValueError("Invalid outcome numeric field")
    if outcome["risk_points"] <= 0 or outcome["entry_spx"] <= 0 or outcome["exit_spx"] <= 0:
        raise ValueError("Invalid outcome prices")
    if not math.isclose(outcome["signed_points"] / outcome["risk_points"],
                        outcome["r_multiple"], rel_tol=1e-7, abs_tol=1e-7):
        raise ValueError("Inconsistent outcome")
    if not isinstance(outcome.get("ambiguous_stop_target"), bool):
        raise ValueError("Invalid ambiguity flag")
    sign = 1 if candidate["direction"] == "UP" else -1
    stop = candidate["range_low"] if sign == 1 else candidate["range_high"]
    if (not math.isclose(sign * (outcome["exit_spx"] - outcome["entry_spx"]),
                         outcome["signed_points"], rel_tol=1e-7, abs_tol=1e-7)
            or not math.isclose(sign * (outcome["entry_spx"] - stop),
                                outcome["risk_points"], rel_tol=1e-7, abs_tol=1e-7)):
        raise ValueError("Outcome disagrees with direction or prices")
    entry = _stamp(candidate["entry_time"])
    exit_bar = _stamp(outcome["exit_bar"])
    if (_stamp(outcome["entry_time"]) != entry
            or not entry <= exit_bar < entry + timedelta(minutes=30)
            or exit_bar.second != 0 or exit_bar.microsecond != 0
            or _stamp(outcome["observed_at"]) < exit_bar + timedelta(minutes=1)):
        raise ValueError("Non-causal outcome")


def _orb_view(record, day, as_of):
    """Copy/filter evidence and reconcile pending outcomes without mutating disk."""
    validate_record(record, day)
    row = deepcopy(record)
    row["bars"] = [b for b in row["bars"] if _stamp(b["available_at"]) <= as_of
                   and _stamp(b["timestamp"]) + timedelta(minutes=1) <= as_of]
    cutoff_snapshot = any(_stamp(row[key]) > as_of for key in
                          ("last_success_at", "last_poll_at", "reconciled_at") if row.get(key) is not None)
    # Latest mutable counters/status cannot be reconstructed for an earlier cutoff.
    row["snapshot_withheld"] = cutoff_snapshot
    if cutoff_snapshot:
        row["status"] = "as_of_status_unavailable"
    else:
        for key in ("polls", "fetch_failures", "rejected_bars"):
            if not _integer(row.get(key, 0)):
                raise ValueError("Invalid counter")
        if row.get("fetch_failures", 0) > row.get("polls", 0):
            raise ValueError("Inconsistent counters")
    candidate = row.get("candidate")
    if candidate and _stamp(candidate["available_at"]) > as_of:
        row["candidate"] = candidate = None
        row["outcome"] = None
    if not candidate:
        row["outcome"] = None
        return row
    if _stamp(candidate["available_at"]).astimezone(ET).date() != day:
        raise ValueError("Wrong candidate date")
    if _stamp(candidate["signal_bar"]) + timedelta(minutes=1) > _stamp(candidate["available_at"]):
        raise ValueError("Future signal")
    if candidate["status"] == "missed":
        row["status"], row["outcome"] = "missed", None
        return row
    entry = _stamp(candidate["entry_time"])
    if entry <= _stamp(candidate["available_at"]) or entry.astimezone(ET).date() != day:
        raise ValueError("Invalid entry time")
    for key in ("range_high", "range_low"):
        if not _number(candidate.get(key)) or candidate[key] <= 0:
            raise ValueError("Invalid candidate range")
    if candidate["range_high"] <= candidate["range_low"]:
        raise ValueError("Invalid candidate range")
    outcome = row.get("outcome")
    row["reconciled_in_memory"] = False
    row["terminal_time_unknown"] = False
    # Final missing/invalid outcomes are NOT pending. Later backfills must not
    # silently turn a finalized collection failure into a profitable trade.
    if outcome is not None:
        status = outcome.get("status")
        if status == "complete":
            _complete_outcome(outcome, candidate)
            if _stamp(outcome["observed_at"]) > as_of:
                outcome = None
        elif status in {"missing", "invalid_entry"}:
            # Schema 1 omits their finalization timestamp. A latest snapshot by
            # cutoff proves the result existed by then; a future snapshot cannot.
            has_snapshot_time = any(row.get(k) for k in ("last_poll_at", "last_success_at", "reconciled_at"))
            if cutoff_snapshot or not has_snapshot_time:
                outcome = None
                row["terminal_time_unknown"] = True
        else:
            raise ValueError("Unknown persisted outcome status")
    if outcome is None and not row["terminal_time_unknown"]:
        known = {_stamp(b["timestamp"]): b for b in row["bars"]}
        outcome = evaluate_outcome(candidate, known, as_of)
        row["reconciled_in_memory"] = outcome is not None
    if outcome and outcome.get("status") == "complete":
        _complete_outcome(outcome, candidate)
    row["outcome"] = outcome
    row["status"] = outcome["status"] if outcome else "outcome_pending"
    return row


def _flow_status(candidate):
    flow = candidate.get("flow")
    value = flow.get("status") if isinstance(flow, dict) else None
    return value if isinstance(value, str) and value in {"qualifying", "filtered", "missing"} else "unknown"


def _performance(label, rows):
    rs = [r["outcome"]["r_multiple"] for r in rows]
    points = [r["outcome"]["signed_points"] for r in rows]
    return (f"{label}: n={len(rs)} wins={sum(r > 0 for r in rs)} "
            f"losses={sum(r < 0 for r in rs)} flat={sum(r == 0 for r in rs)} "
            f"mean_R={sum(rs)/len(rs):+.3f} total_R={sum(rs):+.3f} "
            f"signed_SPX_points={sum(points):+.3f}" if rs else f"{label}: n=0; results=n/a")


def _comparison(label, rows):
    baseline = [r["outcome"]["r_multiple"] for r in rows]
    policy = [r["outcome"]["r_multiple"] if _flow_status(r["candidate"]) == "qualifying" else 0
              for r in rows]
    if not baseline:
        return f"{label}: eligible_sessions=0; baseline_R/session=n/a policy_R/session=n/a delta=n/a"
    n = len(baseline)
    return (f"{label}: eligible_sessions={n} "
            f"baseline_R/session={sum(baseline)/n:+.3f} "
            f"policy_R/session={sum(policy)/n:+.3f} "
            f"delta={(sum(policy)-sum(baseline))/n:+.3f} "
            f"policy_total_R={sum(policy):+.3f}")


def build_report(directory: str | Path, start: date, days: int, as_of: datetime) -> str:
    """Return safe plain text. `as_of` is an aware instant, not an inclusive date."""
    if not isinstance(start, date) or isinstance(start, datetime):
        raise ValueError("start must be a date")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3660:
        raise ValueError("days must be between 1 and 3660")
    as_of = utc(as_of)
    end = start + timedelta(days=days)
    directory = Path(directory)
    elapsed_end = min(end, as_of.astimezone(ET).date() + timedelta(days=1))
    sessions = [d for d in _days(start, elapsed_end) if market_calendar.is_trading_day(d)
                and session_time(d, 9, 30) <= as_of]
    all_sessions = [d for d in _days(start, end) if market_calendar.is_trading_day(d)]
    partial = [d for d in sessions if as_of < session_time(d, *market_calendar.close_time(d))]
    lines = ["THETADATA AUDIT - OBSERVED EVIDENCE, NOT A SUBSCRIPTION VERDICT",
             f"Window (ET start dates): {start} through {end - timedelta(days=1)} ({days} calendar days)",
             f"As of: {as_of.astimezone(ET).isoformat()}",
             f"Trading sessions: elapsed={len(sessions)} planned={len(all_sessions)} "
             f"completed={len(sessions)-len(partial)} partial={len(partial)}",
             "Current session is PARTIAL; only completed, received bars count." if partial else
             "No in-progress session in the elapsed reporting window.", "", "REQUEST USAGE / RELIABILITY"]
    pairs, diag = _load_usage(directory, start, end, as_of)
    lines.append("Overall: " + _usage_stats(pairs))
    for field in ("product", "purpose", "endpoint"):
        groups = defaultdict(list)
        for pair in pairs:
            groups[(pair[0] or pair[1])[field]].append(pair)
        lines.append(f"By {field}:")
        lines.extend(f"  {name}: {_usage_stats(group)}" for name, group in sorted(groups.items()))
        if not groups:
            lines.append("  No observed events.")
    lines.append("Telemetry integrity: " + " ".join(f"{k}={diag[k]}" for k in (
        "files", "malformed", "unreadable_files", "duplicates", "conflicts",
        "inconsistent_pairs", "outside_window", "after_cutoff")))
    if not pairs:
        lines.append("NO TELEMETRY EVIDENCE: this is not proof of zero usage; tracking may be disabled, "
                     "the bot offline, the directory wrong, or writes unavailable.")
    lines += ["Attempts count saved starts; orphan finishes are shown, not invented starts. "
              "Unmatched starts mean pending/interrupted/unrecorded completions, NOT proven hangs.",
              "Latency p50/p95 uses nearest-rank across all observed finishes (including failures). "
              "Failure-rate denominator is completions. Empty/unknown/raw rows describe successful responses only.",
              "Duplicate metadata events are counted once; conflicting duplicates keep the first valid event "
              "and require integrity review before trusting totals.",
              "SDK calls are not vendor billing units; raw rows can repeat across windows. "
              "Request success does not prove normalized, fresh, usable data.", "", "SESSION COVERAGE / COLLECTION HEALTH"]
    views, missing, corrupt, present = [], [], [], 0
    for day in sessions:
        path = directory / f"orb-{day.isoformat()}.json"
        if not path.exists():
            missing.append(day)
            continue
        present += 1
        try:
            with path.open(encoding="utf-8") as handle:
                view = _orb_view(json.load(handle), day, as_of)
            views.append(view)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            corrupt.append(day)
    expected = sum(_expected_minutes(d, as_of) for d in sessions)
    available = sum(len(row["bars"]) for row in views)
    lines.append(f"ORB files: expected={len(sessions)} present={present} valid={len(views)} "
                 f"missing={len(missing)} corrupt_or_incompatible={len(corrupt)}")
    lines.append(f"Complete received minute bars: {available}/{expected} ({_percent(available, expected)}); "
                 "denominator includes missing/corrupt sessions and respects early closes.")
    for label, values in (("Missing sessions", missing), ("Corrupt/incompatible sessions", corrupt)):
        if values:
            lines.append(label + ": " + ", ".join(d.isoformat() for d in values))
    snapshots = [r for r in views if not r["snapshot_withheld"]]
    lines.append(f"Heartbeat snapshots: polls={sum(r.get('polls', 0) for r in snapshots)} "
                 f"fetch_failures={sum(r.get('fetch_failures', 0) for r in snapshots)} "
                 f"rejected_bars={sum(r.get('rejected_bars', 0) for r in snapshots)} "
                 f"request_started={sum(r.get('poll_status') == 'request_started' for r in snapshots)} "
                 f"failed={sum(r.get('poll_status') == 'failed' for r in snapshots)} "
                 f"future_snapshots_withheld={len(views)-len(snapshots)}")
    lines.append("Heartbeats are latest file snapshots, not an uptime guarantee. Historical backfill coverage "
                 "does not prove live availability; future-dated mutable counters/status are withheld.")
    for row in views:
        day = date.fromisoformat(row["session_date"])
        status = row.get("status")
        status = status if isinstance(status, str) and status in STATUSES else "unknown"
        lines.append(f"  {day}: bars={len(row['bars'])}/{_expected_minutes(day, as_of)} status={status}")
    lines += ["", "FROZEN ORB UNDERLYING PAPER RESULTS"]
    candidates = [r for r in views if r.get("candidate")]
    prospective = [r for r in candidates if r["candidate"]["status"] == "prospective"]
    complete = [r for r in prospective if r.get("outcome") and r["outcome"].get("status") == "complete"]
    counts = Counter(r["outcome"]["status"] if r.get("outcome") else "pending" for r in prospective)
    lines.append(f"Candidates: prospective={len(prospective)} missed={len(candidates)-len(prospective)} "
                 f"no_candidate={len(views)-len(candidates)} "
                 f"no_breakout={sum(r.get('status') == 'no_breakout' for r in views)}")
    lines.append(f"Prospective outcomes: complete={counts['complete']} pending={counts['pending']} "
                 f"missing={counts['missing']} invalid_entry={counts['invalid_entry']} "
                 f"reconciled_in_memory={sum(r.get('reconciled_in_memory', False) for r in prospective)} "
                 f"terminal_time_unknown={sum(r.get('terminal_time_unknown', False) for r in prospective)}")
    lines.append("Finalized missing/invalid outcomes stay finalized, even after later backfills. "
                 "Schema-1 terminal outcomes without provable as-of finalization remain pending/"
                 "terminal_time_unknown and are excluded from performance.")
    lines.append(_performance("Price-only baseline / all completed candidates", complete))
    lines.append(f"Ambiguous stop/target bars resolved pessimistically: "
                 f"{sum(r['outcome']['ambiguous_stop_target'] for r in complete)}")
    flow_counts = Counter(_flow_status(r["candidate"]) for r in prospective)
    lines.append("Flow availability / ALL prospective candidates: " + " ".join(
        f"{key}={flow_counts[key]}" for key in ("qualifying", "filtered", "missing", "unknown")))
    paired = [r for r in complete if _flow_status(r["candidate"]) in {"qualifying", "filtered"}]
    lines.append(_performance("Paired evaluable-flow baseline", paired))
    for state in ("qualifying", "filtered", "missing", "unknown"):
        lines.append(_performance(f"Flow {state} / completed candidates",
                                  [r for r in complete if _flow_status(r["candidate"]) == state]))
    lines.append(_comparison("Retained flow policy", paired))
    lines.append("Policy denominator is completed, evaluable-flow candidate sessions ONLY (one candidate/day): "
                 "qualifying keeps its R; filtered contributes zero R. Missing/unknown flow and incomplete "
                 "outcomes are excluded, NOT scored zero or treated as evidence against flow. "
                 "This is not an all-calendar-session return.")
    # Split by elapsed expected sessions, not by selectively available winners.
    split = (len(sessions) + 1) // 2
    for name, subset in (("First chronological half", sessions[:split]),
                         ("Second chronological half", sessions[split:])):
        dates = {d.isoformat() for d in subset}
        lines.append(f"{name}: expected_sessions={len(subset)}")
        lines.append("  " + _performance("Price-only", [r for r in complete if r['session_date'] in dates]))
        lines.append("  " + _comparison("Paired policy", [r for r in paired if r['session_date'] in dates]))
    lines += ["Halves are descriptive stability checks, not independent validation, significance tests, "
              "or a license to tune thresholds on this month."]
    lines += context_report(directory, start, end, as_of, usage_pairs=pairs)
    lines += ["", "DECISION INTERPRETATION / NEXT ACTIONS",
              "1. Establish capture first: investigate missing sessions, malformed/conflicting events, "
              "unmatched starts, stale/missing flow and incomplete outcomes before interpreting performance.",
              f"2. Index data has observable operational use in {len(prospective)} prospective ORB candidates "
              f"and {len(complete)} complete outcomes. Audit-generated demand is research overhead, "
              "not proof the index plan is cost-effective or that existing alerts depend on it.",
              f"3. Options' incremental setup value remains UNPROVEN: only {len(paired)} completed paired "
              "candidate sessions are evaluable here. Inspect retained AND rejected candidates, coverage, "
              "policy R/session and chronological stability; missing flow cannot answer the value question.",
              "4. Review cheaper plan endpoint entitlements and replacement data freshness/coverage before "
              "a subscription change. This report makes no automatic keep/cancel recommendation and "
              "does not measure invoice cost, endpoint entitlement or all strategies using the account.",
              "5. Strategy is a frozen first-close 15m ORB, at most one candidate/session, next minute open "
              "strictly after decision availability, opposite-range stop, 1R target, 30m maximum hold. "
              "It is paper research, not a live recommendation or established profitable strategy.",
              "All results are underlying SPX points/R, NOT SPX 0DTE option P&L. Fees, option spreads, "
              "slippage, IV and decay are not applied. OHLC ambiguity and latency limit execution realism. "
              "MFE/MAE in source records are full-bar upper bounds, not exact intrabar paths.",
              "Read-only report: pending outcomes are reconciled in memory from saved bars; "
              "no files were rewritten and no paid endpoints were called."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--directory", type=Path, default=Path(THETADATA_DATA_DIR) / "audit")
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Inclusive completed Eastern date; default is current instant (may be partial)")
    args = parser.parse_args(argv)
    if args.as_of:
        cutoff = ET.localize(datetime.combine(args.as_of + timedelta(days=1), time.min)) - timedelta(microseconds=1)
    else:
        cutoff = datetime.now(UTC)
    try:
        print(build_report(args.directory, args.start, args.days, cutoff), end="")
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
