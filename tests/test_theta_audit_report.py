"""Offline report tests: denominators, temporal cutoffs, integrity and privacy."""
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.market_calendar import ET
from report_theta_audit import build_report, main
from zero_dte.orb_audit import new_record, session_time

DAY = date(2026, 9, 8)  # Tuesday after Labor Day.


def end_of(day):
    return ET.localize(datetime(day.year, day.month, day.day, 23, 59, 59, 999999))


def usage(identity, event="start", *, day=DAY, endpoint="index_history_ohlc",
          purpose="audit", success=True, rows=10, elapsed=100):
    started = session_time(day, 10)
    row = {"schema": 1, "request_id": identity, "event": event, "endpoint": endpoint,
           "product": "index" if endpoint.startswith("index_") else "options",
           "purpose": purpose, "started_at": started.isoformat()}
    if event == "finish":
        row.update(finished_at=(started + timedelta(milliseconds=elapsed)).isoformat(),
                   elapsed_ms=elapsed, success=success, rows=rows, error_class=None)
    return row


def candidate(day=DAY, *, flow="qualifying", missed=False):
    available = session_time(day, 9, 46) + timedelta(seconds=5)
    return {"status": "missed" if missed else "prospective", "reason": "fixture",
            "signal_bar": session_time(day, 9, 45).isoformat(), "signal_close": 102,
            "available_at": available.isoformat(), "direction": "UP",
            "range_high": 101, "range_low": 100,
            "entry_time": None if missed else session_time(day, 9, 47).isoformat(),
            "flow": {"status": flow}}


def record(day=DAY, *, flow="qualifying", r=1, pending=False, missed=False):
    row = new_record(day)
    row.update(candidate=candidate(day, flow=flow, missed=missed), polls=30,
               fetch_failures=2, rejected_bars=1,
               last_poll_at=session_time(day, 11).isoformat(),
               last_success_at=session_time(day, 11).isoformat(), poll_status="success")
    if not missed:
        # Entry 102; stop 100; target 104. All outcomes complete in entry minute.
        stamp = session_time(day, 9, 47)
        row["bars"] = [{"timestamp": stamp.isoformat(),
                        "available_at": (stamp + timedelta(minutes=1, seconds=2)).isoformat(),
                        "open": 102., "high": 104. if r > 0 else 103.,
                        "low": 101. if r > 0 else 100., "close": 102.}]
        if not pending:
            row["outcome"] = {"status": "complete", "reason": "target" if r > 0 else "stop",
                              "signed_points": 2 * r, "r_multiple": r, "risk_points": 2,
                              "entry_time": stamp.isoformat(), "entry_spx": 102, "exit_spx": 102 + 2 * r,
                              "observed_at": session_time(day, 9, 49).isoformat(),
                              "exit_bar": stamp.isoformat(), "ambiguous_stop_target": False}
        row["status"] = "outcome_pending" if pending else "complete"
    else:
        row["status"] = "missed"
    return row


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write_usage(self, rows, day=DAY):
        path = self.directory / f"usage-{day}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def write_orb(self, row):
        path = self.directory / f"orb-{row['session_date']}.json"
        path.write_text(json.dumps(row), encoding="utf-8")
        return path

    def report(self, *, start=DAY, days=1, as_of=None):
        return build_report(self.directory, start, days, as_of or end_of(start + timedelta(days=days-1)))

    def test_no_data_does_not_claim_no_usage_or_cancel(self):
        text = self.report()
        self.assertIn("NO TELEMETRY EVIDENCE: this is not proof of zero usage", text)
        self.assertIn("expected=1 present=0 valid=0 missing=1", text)
        self.assertIn("0/390 (0.0%)", text)
        self.assertIn("policy_R/session=n/a", text)
        self.assertIn("no automatic keep/cancel", text)
        self.assertIn("incremental setup value remains UNPROVEN", text)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_attempts_completions_denominators_percentiles_and_grouping(self):
        rows = []
        for i, (latency, success, count) in enumerate([(10, True, 0), (20, True, None),
                                                      (30, False, None), (10000, True, 5)]):
            rows += [usage(str(i)), usage(str(i), "finish", elapsed=latency, success=success, rows=count)]
        rows += [usage("pending"), usage("orphan", "finish", endpoint="option_history_trade_quote",
                                         purpose="spx_flow", elapsed=50)]
        self.write_usage(rows)
        text = self.report()
        overall = next(line for line in text.splitlines() if line.startswith("Overall:"))
        self.assertIn("attempts=5 completions=5 failures=1 failure_rate=20.0%", overall)
        self.assertIn("empty=1 unknown_rows=1 raw_rows=15", overall)
        self.assertIn("p50_ms=30.0 p95_ms=10000.0", overall)
        self.assertIn("unmatched_starts=1 orphan_finishes=1", overall)
        self.assertIn("  options: attempts=0 completions=1", text)
        self.assertIn("  spx_flow: attempts=0 completions=1", text)
        self.assertIn("  option_history_trade_quote:", text)

    def test_dedup_conflicts_malformed_and_secret_redaction(self):
        begin, finish = usage("a"), usage("a", "finish")
        duplicate = dict(finish, arbitrary="SECRET-DATA")
        conflict = dict(finish, rows=99)
        unknown = dict(usage("unknown"), endpoint="SECRET-ENDPOINT")
        private = usage("private", purpose="SECRET-PURPOSE")
        private["api_key"] = "SECRET-API-KEY"
        path = self.write_usage([begin, finish, begin, duplicate, conflict, unknown, private])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"credential":"SECRET-TRUNCATED"\n')
            handle.write('[]\n')
        text = self.report()
        self.assertIn("attempts=2 completions=1", text)
        self.assertIn("malformed=3", text)
        self.assertIn("duplicates=2 conflicts=1", text)
        self.assertNotIn("SECRET", text)
        self.assertIn("  unspecified:", text)

    def test_inconsistent_pairs_not_joined(self):
        self.write_usage([usage("a"), usage("a", "finish", purpose="diagnostics")])
        text = self.report()
        self.assertIn("attempts=1 completions=0", text)
        self.assertIn("inconsistent_pairs=1", text)
        self.assertIn("unmatched_starts=1", text)

    def test_invalid_numeric_events_rejected(self):
        rows = [usage("good"), usage("good", "finish")]
        for i, changes in enumerate([{"elapsed_ms": float("nan")}, {"rows": -1},
                                      {"rows": True}, {"success": "true"},
                                      {"elapsed_ms": float("inf")}, {"elapsed_ms": -1}]):
            rows.append(dict(usage(str(i), "finish"), **changes))
        self.write_usage(rows)
        text = self.report()
        self.assertIn("malformed=6", text)
        self.assertIn("attempts=1 completions=1", text)

    def test_start_cohort_window_and_future_finish(self):
        start = usage("late")
        start["started_at"] = end_of(DAY).isoformat()
        finish = dict(start, event="finish", finished_at=(end_of(DAY) + timedelta(seconds=5)).isoformat(),
                      elapsed_ms=5000, rows=1, success=True)
        self.write_usage([start, finish, usage("wrong", day=DAY-timedelta(days=1))])
        self.write_usage([usage("tomorrow", day=DAY+timedelta(days=1))], DAY+timedelta(days=1))
        today = self.report()
        self.assertIn("attempts=1 completions=0", today)
        self.assertIn("outside_window=1 after_cutoff=1", today)
        tomorrow = self.report(as_of=end_of(DAY+timedelta(days=1)))
        self.assertIn("attempts=1 completions=1", tomorrow)
        self.assertNotIn("attempts=2", tomorrow)

    def test_holiday_weekend_and_early_close_coverage(self):
        # Thanksgiving excluded; Friday Nov 27 closes at 13:00.
        start = date(2026, 11, 26)
        text = self.report(start=start, days=4)
        self.assertIn("elapsed=1 planned=1 completed=1 partial=0", text)
        self.assertIn("0/210 (0.0%)", text)

    def test_partial_session_uses_only_received_complete_bars(self):
        row = new_record(DAY)
        now = session_time(DAY, 9, 32) + timedelta(seconds=30)
        for minute, received in [(30, 31), (31, 33)]:
            stamp = session_time(DAY, 9, minute)
            row["bars"].append({"timestamp": stamp.isoformat(), "available_at": session_time(DAY, 9, received).isoformat(),
                                "open": 100, "high": 101, "low": 99, "close": 100})
        row.update(polls=5, fetch_failures=1, last_poll_at=session_time(DAY, 10).isoformat(), status="no_breakout")
        self.write_orb(row)
        text = self.report(as_of=now)
        self.assertIn("PARTIAL", text)
        self.assertIn("1/2 (50.0%)", text)
        self.assertIn("future_snapshots_withheld=1", text)
        self.assertIn("polls=0 fetch_failures=0", text)
        self.assertIn("no_breakout=0", text)

    def test_premarket_and_future_window_have_no_due_session(self):
        text = self.report(as_of=session_time(DAY, 8))
        self.assertIn("elapsed=0 planned=1", text)
        self.assertIn("expected=0 present=0", text)
        self.assertIn("0/0 (n/a)", text)
        text = self.report(start=DAY+timedelta(days=1), as_of=end_of(DAY))
        self.assertIn("elapsed=0", text)

    def test_flow_policy_common_denominator_and_chronological_halves(self):
        # Four consecutive sessions: keep +1, reject -1, missing +1, unknown -1.
        for i, (flow, r) in enumerate([("qualifying", 1), ("filtered", -1),
                                       ("missing", 1), ("bad-status", -1)]):
            self.write_orb(record(DAY+timedelta(days=i), flow=flow, r=r))
        text = self.report(days=4)
        self.assertIn("Price-only baseline / all completed candidates: n=4 wins=2 losses=2 flat=0 mean_R=+0.000", text)
        self.assertIn("qualifying=1 filtered=1 missing=1 unknown=1", text)
        self.assertIn("Paired evaluable-flow baseline: n=2", text)
        self.assertIn("eligible_sessions=2 baseline_R/session=+0.000 policy_R/session=+0.500 delta=+0.500", text)
        self.assertIn("Flow filtered / completed candidates: n=1 wins=0 losses=1", text)
        self.assertIn("First chronological half: expected_sessions=2", text)
        self.assertIn("Second chronological half: expected_sessions=2", text)
        self.assertIn("polls=120 fetch_failures=8 rejected_bars=4", text)
        self.assertNotIn("bad-status", text)

    def test_pending_reconciled_to_complete_without_writing(self):
        path = self.write_orb(record(pending=True))
        before = path.read_bytes()
        text = self.report()
        self.assertIn("complete=1 pending=0 missing=0", text)
        self.assertIn("reconciled_in_memory=1", text)
        self.assertIn("mean_R=+1.000", text)
        self.assertEqual(before, path.read_bytes())

    def test_last_day_outage_reconciles_missing_not_dropped(self):
        row = record(pending=True, flow="filtered")
        row["bars"] = []
        path = self.write_orb(row)
        before = path.read_bytes()
        text = self.report()
        self.assertIn("prospective=1 missed=0", text)
        self.assertIn("complete=0 pending=0 missing=1", text)
        self.assertIn("filtered=1 missing=0 unknown=0", text)
        self.assertIn("Retained flow policy: eligible_sessions=0", text)
        self.assertEqual(path.read_bytes(), before)

    def test_pending_stays_pending_before_horizon_and_ignores_later_result(self):
        self.write_orb(record())
        text = self.report(as_of=session_time(DAY, 9, 47) + timedelta(seconds=30))
        self.assertIn("complete=0 pending=1 missing=0", text)
        self.assertIn("eligible_sessions=0", text)

    def test_future_candidate_not_counted(self):
        self.write_orb(record())
        text = self.report(as_of=session_time(DAY, 9, 40))
        self.assertIn("prospective=0 missed=0 no_candidate=1", text)
        self.assertIn("future_snapshots_withheld=1", text)

    def test_missed_no_breakout_corrupt_unknown_status_and_privacy(self):
        self.write_orb(record(missed=True))
        row = new_record(DAY+timedelta(days=1))
        row["status"] = "no_breakout"
        self.write_orb(row)
        row = new_record(DAY+timedelta(days=2))
        row["status"] = "SECRET-STATUS"
        self.write_orb(row)
        path = self.directory / f"orb-{DAY+timedelta(days=3)}.json"
        path.write_text('{"secret":"SECRET-CORRUPT"', encoding="utf-8")
        text = self.report(days=4)
        self.assertIn("present=4 valid=3 missing=0 corrupt_or_incompatible=1", text)
        self.assertIn("prospective=0 missed=1 no_candidate=2 no_breakout=1", text)
        self.assertIn("status=unknown", text)
        self.assertNotIn("SECRET", text)

    def test_malformed_record_does_not_abort_other_sessions(self):
        row = record()
        row["outcome"]["r_multiple"] = float("nan")
        self.write_orb(row)
        self.write_orb(record(DAY+timedelta(days=1)))
        text = self.report(days=2)
        self.assertIn("present=2 valid=1 missing=0 corrupt_or_incompatible=1", text)
        self.assertIn("complete=1 pending=0", text)

    def test_unknown_flow_structures_never_render_arbitrary_data(self):
        row = record()
        row["candidate"]["flow"] = {"status": ["SECRET-FLOW"]}
        self.write_orb(row)
        text = self.report()
        self.assertIn("missing=0 unknown=1", text)
        self.assertNotIn("SECRET", text)

    def test_ambiguous_bar_and_invalid_entry_reconciliation(self):
        row = record(pending=True)
        row["bars"][0]["low"] = 100
        self.write_orb(row)
        text = self.report()
        self.assertIn("mean_R=-1.000", text)
        self.assertIn("resolved pessimistically: 1", text)
        row["bars"][0].update(open=100, low=99)
        self.write_orb(row)
        text = self.report()
        self.assertIn("invalid_entry=1", text)
        self.assertIn("eligible_sessions=0", text)

    def test_cli_as_of_inclusive_et_day_and_default_partial(self):
        output = io.StringIO()
        with redirect_stdout(output):
            main(["--start", str(DAY), "--days", "1", "--directory", str(self.directory),
                  "--as-of", str(DAY)])
        self.assertIn("23:59:59.999999-04:00", output.getvalue())
        self.assertIn("0/390", output.getvalue())
        now = session_time(DAY, 10)
        with patch("report_theta_audit.datetime") as clock:
            clock.now.return_value = now
            # Avoid patching isinstance's class: mock build_report here.
            with patch("report_theta_audit.build_report", return_value="partial\n") as build:
                with redirect_stdout(io.StringIO()):
                    main(["--start", str(DAY), "--directory", str(self.directory)])
                self.assertEqual(build.call_args.args[-1], now)

    def test_final_missing_not_upgraded_by_later_backfill(self):
        row = record(pending=True)
        row.update(outcome={"status": "missing", "reason": "missing_entry_bar"}, status="missing")
        row["bars"][0]["available_at"] = session_time(DAY, 11).isoformat()
        path = self.write_orb(row)
        before = path.read_bytes()
        text = self.report()
        self.assertIn("complete=0 pending=0 missing=1", text)
        self.assertIn("reconciled_in_memory=0", text)
        self.assertIn("Price-only baseline / all completed candidates: n=0", text)
        self.assertEqual(path.read_bytes(), before)

    def test_terminal_finalization_unknown_at_historical_cutoff(self):
        row = record(pending=True)
        row.update(outcome={"status": "missing", "reason": "missing_outcome_bar"}, status="missing")
        # Bars alone would yield +1R, but must not revise this terminal record.
        self.write_orb(row)
        text = self.report(as_of=session_time(DAY, 10))
        self.assertIn("complete=0 pending=1 missing=0", text)
        self.assertIn("terminal_time_unknown=1", text)
        self.assertIn("reconciled_in_memory=0", text)
        # Overnight reconciliation timestamp must also respect as-of.
        row["last_poll_at"] = row["last_success_at"] = session_time(DAY, 9, 50).isoformat()
        row["reconciled_at"] = session_time(DAY+timedelta(days=1), 9, 30).isoformat()
        self.write_orb(row)
        self.assertIn("terminal_time_unknown=1", self.report())

    def test_inconsistent_outcome_prices_and_timing_quarantined(self):
        changes = [{"exit_spx": 100}, {"risk_points": 1, "r_multiple": 2},
                   {"exit_bar": session_time(DAY, 9, 40).isoformat()},
                   {"exit_bar": session_time(DAY, 10, 17).isoformat(),
                    "observed_at": session_time(DAY, 10, 18).isoformat()},
                   {"observed_at": session_time(DAY, 9, 47).isoformat()},
                   {"entry_time": session_time(DAY, 9, 46).isoformat()}]
        for change in changes:
            with self.subTest(change=change):
                row = record()
                row["outcome"].update(change)
                self.write_orb(row)
                text = self.report()
                self.assertIn("valid=0 missing=0 corrupt_or_incompatible=1", text)
                self.assertIn("Price-only baseline / all completed candidates: n=0", text)

    def test_deeply_nested_json_is_quarantined_without_abort(self):
        nested = "[" * 1100 + "0" + "]" * 1100
        path = self.write_usage([usage("valid"), usage("valid", "finish")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(nested + "\n")
        (self.directory / f"orb-{DAY}.json").write_text(nested, encoding="utf-8")
        text = self.report()
        self.assertIn("attempts=1 completions=1", text)
        self.assertIn("malformed=1", text)
        self.assertIn("corrupt_or_incompatible=1", text)

    def test_invalid_arguments_and_unreadable_data(self):
        for days in (0, -1, True, 4000):
            with self.assertRaises(ValueError):
                build_report(self.directory, DAY, days, end_of(DAY))
        with self.assertRaises(ValueError):
            build_report(self.directory, DAY, 1, datetime(2026, 9, 8))
        # A directory in place of a data file must not kill the report.
        (self.directory / f"usage-{DAY}.jsonl").mkdir()
        (self.directory / f"orb-{DAY}.json").mkdir()
        text = self.report()
        self.assertIn("unreadable_files=1", text)
        self.assertIn("corrupt_or_incompatible=1", text)


if __name__ == "__main__":
    unittest.main()
