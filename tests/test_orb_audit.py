"""Offline prospective timing, conservative OHLC, persistence and expiry tests."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import config
from core.thetadata_client import IndexBar
from zero_dte.audit_cog import AuditCog, capture_flow, collection_open
from zero_dte.orb_audit import (
    evaluate_outcome, flow_filter, new_record, session_time, update_record, utc,
)
from zero_dte.turning_points import FlowSummary

DAY = date(2026, 9, 4)


def at(hour, minute=0, second=0):
    return session_time(DAY, hour, minute) + timedelta(seconds=second)


def bar(stamp, o=100, h=101, l=99, c=100):
    return IndexBar(stamp, o, h, l, c)


def opening():
    return [bar(at(9, 30) + timedelta(minutes=i)) for i in range(15)]


def breakout():
    return bar(at(9, 45), 100, 103, 100, 102)


def candidate(direction="UP"):
    return {"status": "prospective", "direction": direction, "range_low": 99,
            "range_high": 101, "available_at": at(9, 46, 10).isoformat(),
            "entry_time": at(9, 47).isoformat()}


def known(*bars):
    return {b.timestamp: {"open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for b in bars}


def flow(ratio=.2, **kwargs):
    return {"sample_at": at(9, 46).isoformat(), "captured_at": at(9, 46, 5).isoformat(),
            "latest_trade_at": at(9, 45, 55).isoformat(),
            "total_trades": 100, "classified_trades": 30, "ratio": ratio, **kwargs}


class ORBEvaluatorTests(unittest.TestCase):
    def test_timing_entry_strictly_after_receipt_and_one_candidate(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(9, 46, 10), flow())
        first = copy.deepcopy(record["candidate"])
        self.assertEqual(first["status"], "prospective")
        self.assertEqual(first["entry_time"], at(9, 47).isoformat())
        self.assertEqual(first["flow"]["status"], "qualifying")
        update_record(record, [bar(at(9, 46), 100, 100, 97, 98)], at(9, 47, 10))
        self.assertEqual(record["candidate"], first)
        exact = new_record(DAY)
        update_record(exact, opening() + [breakout()], at(9, 46))
        self.assertEqual(exact["candidate"]["entry_time"], at(9, 47).isoformat())

    def test_late_startup_and_restart_cannot_relabel_first_break(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(10))
        self.assertEqual(record["candidate"]["status"], "missed")
        self.assertIsNone(record["candidate"]["entry_time"])
        reloaded = json.loads(json.dumps(record))
        update_record(reloaded, [bar(at(10), 102, 104, 101, 103)], at(10, 1))
        self.assertEqual(reloaded["candidate"], record["candidate"])
        self.assertIsNone(reloaded["outcome"])

    def test_missing_opening_and_pre_signal_bars_never_filled(self):
        record = new_record(DAY)
        update_record(record, opening()[1:] + [breakout()], at(11))
        self.assertEqual(record["status"], "missing_opening_range")
        self.assertIsNone(record["candidate"])
        record = new_record(DAY)
        update_record(record, opening() + [bar(at(9, 46), 102, 104, 101, 103)], at(9, 47))
        self.assertEqual(record["status"], "missing_pre_signal_bar")
        self.assertIsNone(record["candidate"])

    def test_complete_valid_unique_bars_only_and_first_seen_immutable(self):
        record = new_record(DAY)
        bad = [bar(at(9, 44)), bar(at(9, 44)), bar(at(9, 45)),
               bar(at(9, 43, 1)), bar(at(9, 42), h=float("nan")),
               bar(at(9, 41), h=98), bar(at(9, 29))]
        update_record(record, [bar(at(9, 30))] + bad, at(9, 45))
        self.assertEqual(len(record["bars"]), 1)
        first = copy.deepcopy(record["bars"][0])
        update_record(record, [bar(at(9, 30), h=200)], at(9, 46))
        self.assertEqual(record["bars"][0], first)
        self.assertGreater(record["rejected_bars"], 0)

    def test_request_cutoff_not_later_receipt_controls_completeness(self):
        record = new_record(DAY)
        update_record(record, opening() + [breakout()], at(9, 46, 1),
                      data_cutoff=at(9, 45, 59))
        self.assertIsNone(record["candidate"])
        self.assertEqual(len(record["bars"]), 15)
        update_record(record, [breakout()], at(9, 46, 20), data_cutoff=at(9, 46))
        self.assertEqual(record["candidate"]["status"], "prospective")
        self.assertEqual(record["candidate"]["available_at"], at(9, 46, 20).isoformat())

    def test_fresh_poll_does_not_make_old_trades_fresh(self):
        stale = flow(latest_trade_at=at(9, 42).isoformat())
        result = flow_filter(stale, at(9, 46, 10), 'UP')
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'stale_or_future_trade_data')
        self.assertEqual(flow_filter(flow(latest_trade_at=None), at(9, 46, 10), 'UP')['status'], 'missing')

    def test_flow_missing_filtered_qualifying_and_freshness(self):
        now = at(9, 46, 10)
        self.assertEqual(flow_filter(None, now, "UP")["status"], "missing")
        self.assertEqual(flow_filter(flow(0), now, "UP")["status"], "filtered")
        self.assertEqual(flow_filter(flow(-.2), now, "DOWN")["status"], "qualifying")
        for f in [flow(sample_at=at(9, 43).isoformat()),
                  flow(sample_at=at(9, 47).isoformat()),
                  flow(captured_at=at(9, 47).isoformat()),
                  flow(classified_trades=24), flow(total_trades=200), flow(float("nan"))]:
            self.assertEqual(flow_filter(f, now, "UP")["status"], "missing")
        self.assertEqual(flow_filter(flow(sample_at=at(9, 44, 10).isoformat()), now, "UP")["status"], "qualifying")

    def test_ambiguous_bar_stop_first(self):
        result = evaluate_outcome(candidate(), known(bar(at(9, 47), 102, 106, 98, 103)), at(9, 48))
        self.assertEqual(result["r_multiple"], -1)
        self.assertTrue(result["ambiguous_stop_target"])
        self.assertTrue(result["not_option_pnl"])
        self.assertEqual(result["mfe_points_upper_bound"], 4)
        self.assertEqual(result["mae_points_upper_bound"], 4)

    def test_gap_stop_adverse_and_target_capped(self):
        first = bar(at(9, 47), 102, 103, 101, 102)
        result = evaluate_outcome(candidate(), known(first, bar(at(9, 48), 97, 100, 96, 98)), at(9, 49))
        self.assertEqual(result["reason"], "gap_stop")
        self.assertLess(result["r_multiple"], -1)
        result = evaluate_outcome(candidate(), known(first, bar(at(9, 48), 107, 108, 98, 104)), at(9, 49))
        self.assertEqual(result["reason"], "gap_target_capped")
        self.assertEqual(result["r_multiple"], 1)
        self.assertFalse(result["ambiguous_stop_target"])

    def test_short_target_and_invalid_gap_entry(self):
        result = evaluate_outcome(candidate("DOWN"), known(bar(at(9, 47), 98, 99, 94, 96)), at(9, 48))
        self.assertEqual(result["r_multiple"], 1)
        result = evaluate_outcome(candidate(), known(bar(at(9, 47), 98, 100, 97, 99)), at(9, 48))
        self.assertEqual(result["status"], "invalid_entry")

    def test_horizon_and_gap_missing_not_forward_filled(self):
        bars = [bar(at(9, 47) + timedelta(minutes=i), 102, 103, 101, 102.5) for i in range(30)]
        self.assertIsNone(evaluate_outcome(candidate(), known(*bars), at(10, 16, 59)))
        result = evaluate_outcome(candidate(), known(*bars), at(10, 17))
        self.assertEqual(result["reason"], "time_exit")
        self.assertEqual(result["signed_points"], .5)
        del bars[5]
        self.assertIsNone(evaluate_outcome(candidate(), known(*bars), at(10, 17)))
        self.assertEqual(evaluate_outcome(candidate(), known(*bars), at(10, 18, 30))["reason"], "missing_outcome_bar")
        self.assertEqual(evaluate_outcome(candidate(), {}, at(10, 19))["reason"], "missing_entry_bar")

    def test_no_breakout_cutoff_and_no_backdated_entry(self):
        record = new_record(DAY)
        bars = opening() + [bar(at(9, 45) + timedelta(minutes=i)) for i in range(74)]
        update_record(record, bars + [bar(at(10, 59), 100, 104, 99, 103)], at(11))
        self.assertEqual(record["status"], "no_breakout")
        self.assertIsNone(record["candidate"])
        c = candidate()
        c["entry_time"] = c["available_at"]
        with self.assertRaises(ValueError):
            evaluate_outcome(c, {}, at(10))


class AuditCogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.start = patch.object(config, "THETA_AUDIT_START_DATE", DAY.isoformat(), create=True)
        self.days = patch.object(config, "THETA_AUDIT_DAYS", 30, create=True)
        self.start.start()
        self.days.start()
        self.addCleanup(self.start.stop)
        self.addCleanup(self.days.stop)
        self.bot = Mock()
        self.bot.get_cog.return_value = None
        self.client = Mock()
        self.client.index_history_ohlc.return_value = opening() + [breakout()]
        self.cog = AuditCog(self.bot, self.client, directory=self.temp.name)

    async def test_poll_persists_restart_and_no_option_calls(self):
        with patch.object(self.cog, "_now", return_value=at(9, 46, 10)):
            await self.cog.poll_once()
        path = Path(self.temp.name) / f"orb-{DAY}.json"
        record = json.loads(path.read_text())
        self.assertEqual(record["candidate"]["status"], "prospective")
        self.assertEqual(record["candidate"]["flow"]["status"], "missing")
        self.assertEqual(record["poll_status"], "success")
        other = AuditCog(self.bot, self.client, directory=self.temp.name)
        with patch.object(other, "_now", return_value=at(10)):
            await other.poll_once()
        self.assertEqual(json.loads(path.read_text())["candidate"], record["candidate"])
        self.assertEqual([c[0] for c in self.client.method_calls], ["index_history_ohlc"] * 2)
        self.bot.get_channel.assert_not_called()

    async def test_expired_disabled_weekend_and_after_close_no_io(self):
        for now in [at(16, 6), at(9) + timedelta(days=1), at(10) + timedelta(days=31)]:
            with patch.object(self.cog, "_now", return_value=now):
                await self.cog.poll_once()
        with patch.object(config, "THETA_AUDIT_START_DATE", ""):
            with patch.object(self.cog, "_now", return_value=at(10)):
                await self.cog.poll_once()
        self.client.index_history_ohlc.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_real_calendar_utc_scheduling_and_close_completion(self):
        from datetime import datetime, timezone
        for instant, expected in [
            ('2026-09-08T10:00:00+00:00', False),  # 06 ET premarket
            ('2026-09-08T17:00:00+00:00', True),   # 13 ET afternoon
            ('2026-12-01T16:30:00+00:00', True),   # 11:30 ET winter
            ('2026-11-27T15:00:00+00:00', True),   # early-close 10 ET
            ('2026-11-27T18:06:00+00:00', False),  # early-close +6m
        ]:
            self.assertEqual(collection_open(datetime.fromisoformat(instant)), expected)
        with patch.object(self.cog, '_now', return_value=at(6)):
            await self.cog.poll_once()
        self.client.index_history_ohlc.assert_not_called()
        self.client.index_history_ohlc.return_value = [bar(at(15, 59))]
        with patch.object(self.cog, '_now', return_value=at(16, 1)):
            await self.cog.poll_once()
        self.assertEqual(self.client.index_history_ohlc.call_args.kwargs['end_time'], '16:00:00')
        self.assertEqual(len(self.cog._load(DAY)['bars']), 1)

    async def test_incremental_requests_resume_at_earliest_gap(self):
        with patch.object(self.cog, '_now', return_value=at(9, 46, 10)):
            await self.cog.poll_once()
        with patch.object(self.cog, '_now', return_value=at(9, 47, 10)):
            await self.cog.poll_once()
        self.assertEqual(self.client.index_history_ohlc.call_args.kwargs['start_time'], '09:46:00')
        record = self.cog._load(DAY)
        record['bars'] = [b for b in record['bars'] if b['timestamp'] != at(9, 40).isoformat()]
        self.cog._save(DAY, record)
        with patch.object(self.cog, '_now', return_value=at(9, 48, 10)):
            await self.cog.poll_once()
        self.assertEqual(self.client.index_history_ohlc.call_args.kwargs['start_time'], '09:40:00')

    async def test_fetch_error_redacted_and_heartbeat_written_before_request(self):
        def fail(*args, **kwargs):
            record = self.cog._load(DAY)
            self.assertEqual(record["poll_status"], "request_started")
            raise RuntimeError("SECRET_API_TOKEN")
        self.client.index_history_ohlc.side_effect = fail
        with patch.object(self.cog, "_now", return_value=at(10)):
            with self.assertLogs("zero_dte.audit_cog", level="WARNING") as logs:
                await self.cog.poll_once()
        self.assertNotIn("SECRET", str(logs.output))
        record = self.cog._load(DAY)
        self.assertEqual(record["fetch_failures"], 1)
        self.assertEqual(record["poll_status"], "failed")
        self.assertNotIn("SECRET", json.dumps(record))

    async def test_corrupt_file_preserved_and_no_request(self):
        path = self.cog._path(DAY)
        path.write_text("{bad json")
        with patch.object(self.cog, "_now", return_value=at(10)):
            with self.assertLogs("zero_dte.audit_cog", level="WARNING"):
                await self.cog.poll_once()
        self.assertEqual(path.read_text(), "{bad json")
        self.client.index_history_ohlc.assert_not_called()

    async def test_response_crossing_minute_cannot_freeze_partial_bar(self):
        clock = [at(9, 45, 59)]
        def respond(*args, **kwargs):
            self.assertEqual(kwargs["end_time"], "09:45:00")
            clock[0] = at(9, 46, 1)
            return opening() + [breakout()]
        self.client.index_history_ohlc.side_effect = respond
        with patch.object(self.cog, "_now", side_effect=lambda: clock[0]):
            await self.cog.poll_once()
        record = self.cog._load(DAY)
        self.assertEqual(len(record["bars"]), 15)
        self.assertIsNone(record["candidate"])

    async def test_expiry_during_success_or_failure_keeps_start_only(self):
        for failure in (False, True):
            clock = [at(9, 46, 10)]
            def respond(*args, **kwargs):
                clock[0] = at(10) + timedelta(days=31)
                if failure:
                    raise RuntimeError("SECRET")
                return opening() + [breakout()]
            self.client.index_history_ohlc.side_effect = respond
            with patch.object(self.cog, "_now", side_effect=lambda: clock[0]):
                with patch("zero_dte.audit_cog.logger"):
                    await self.cog.poll_once()
            record = self.cog._load(DAY)
            self.assertEqual(record["poll_status"], "request_started")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["bars"], [])

    async def test_overnight_restart_marks_pending_outcome_missing(self):
        with patch.object(self.cog, "_now", return_value=at(9, 46, 10)):
            await self.cog.poll_once()
        prior = self.cog._load(DAY)
        self.assertIsNone(prior["outcome"])
        # Tuesday after the Labor Day closure. No historical backfill request.
        now = at(10) + timedelta(days=4)
        self.client.index_history_ohlc.return_value = []
        other = AuditCog(self.bot, self.client, directory=self.temp.name)
        with patch.object(other, "_now", return_value=now):
            await other.poll_once()
        prior = other._load(DAY)
        self.assertEqual(prior["outcome"]["status"], "missing")
        self.assertEqual(prior["outcome"]["reason"], "missing_entry_bar")
        self.assertEqual(self.client.index_history_ohlc.call_args.args[0], now.date())

    async def test_cached_flow_copied_not_recomputed(self):
        self.bot.get_cog.return_value = SimpleNamespace(
            _last_sample_at=at(9, 46), _last_flow=FlowSummary(100, 30, 60, 40, 0, 0, 20))
        snapshot = capture_flow(self.bot, at(9, 46, 5))
        self.assertEqual(snapshot["ratio"], .2)
        self.assertEqual(snapshot["classified_trades"], 30)
        self.bot.get_cog.assert_called_with("SPXFlow")


if __name__ == "__main__":
    unittest.main()
