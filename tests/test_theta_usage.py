"""Offline tests for request accounting, privacy, and audit-window semantics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import pandas as pd

import config
from core import theta_usage as usage
from core.thetadata_client import ThetaDataClient, ThetaDataError


class ThetaUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for target, value in (
            ("THETA_AUDIT_START_DATE", "2026-09-04"),
            ("THETA_AUDIT_DAYS", 30),
        ):
            p = patch.object(config, target, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(usage, "THETADATA_DATA_DIR", self.temp.name)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(usage, "_now", return_value=datetime(2026, 9, 4, 14, tzinfo=timezone.utc))
        self.clock = p.start()
        self.addCleanup(p.stop)
        self.sdk = Mock()
        self.client = ThetaDataClient(api_key="PRIVATE-KEY", client=self.sdk, purpose="spy_setup")

    def records(self):
        return [json.loads(line) for path in sorted(self.directory.glob("audit/*.jsonl"))
                for line in path.read_text().splitlines()]

    def test_pair_raw_count_before_normalization_and_redaction(self):
        self.sdk.stock_history_ohlc.return_value = pd.DataFrame({
            "timestamp": ["2026-09-04T13:30:00+00:00"],
            "open": [100], "high": [101], "low": [99], "close": [100],
            "volume": [1000], "private_payload": ["SECRET-RESPONSE"],
        })
        self.assertEqual(len(self.client.stock_history_ohlc(date(2026, 9, 4))), 1)
        start, finish = self.records()
        self.assertEqual(start["event"], "start")
        self.assertEqual(finish["event"], "finish")
        self.assertEqual(start["request_id"], finish["request_id"])
        self.assertEqual(finish["rows"], 1)
        self.assertTrue(finish["success"])
        self.assertIsNone(finish["error_class"])
        self.assertEqual(finish["product"], "stock")
        self.assertEqual(finish["purpose"], "spy_setup")
        self.assertGreaterEqual(finish["elapsed_ms"], 0)
        self.assertTrue(start["started_at"].endswith("+00:00"))
        text = json.dumps(self.records())
        for secret in ("PRIVATE-KEY", "SECRET-RESPONSE", "private_payload"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(finish), {
            "schema", "event", "request_id", "endpoint", "product", "purpose",
            "started_at", "finished_at", "elapsed_ms", "success", "error_class", "rows",
        })

    def test_normalization_failure_does_not_relabel_successful_request(self):
        self.sdk.stock_history_ohlc.return_value = pd.DataFrame({"private_payload": ["SECRET"]})
        with self.assertRaises(ThetaDataError):
            self.client.stock_history_ohlc(date(2026, 9, 4))
        finish = self.records()[-1]
        self.assertTrue(finish["success"])
        self.assertEqual(finish["rows"], 1)
        self.assertNotIn("SECRET", json.dumps(self.records()))

    def test_sdk_error_preserves_exception_and_records_class_only(self):
        failure = RuntimeError("secret token and raw response")
        self.sdk.stock_history_ohlc.side_effect = failure
        with self.assertRaises(ThetaDataError) as caught:
            self.client.stock_history_ohlc(date(2026, 9, 4))
        self.assertIs(caught.exception.__cause__, failure)
        finish = self.records()[-1]
        self.assertFalse(finish["success"])
        self.assertEqual(finish["error_class"], "RuntimeError")
        self.assertIsNone(finish["rows"])
        self.assertEqual(finish["product"], "stock")
        self.assertNotIn("secret token", json.dumps(self.records()))

    def test_start_is_persisted_before_sdk_runs(self):
        def during_call(**kwargs):
            self.assertEqual([r["event"] for r in self.records()], ["start"])
            return pd.DataFrame()
        self.sdk.stock_history_ohlc.side_effect = during_call
        self.client.stock_history_ohlc(date(2026, 9, 4))
        self.assertEqual([r["event"] for r in self.records()], ["start", "finish"])

    def test_window_et_boundaries_and_invalid_configuration(self):
        for stamp, expected in (
            ("2026-09-04T03:59:59+00:00", False),
            ("2026-09-04T04:00:00+00:00", True),
            ("2026-10-04T03:59:59+00:00", True),
            ("2026-10-04T04:00:00+00:00", False),
        ):
            self.assertEqual(usage.audit_active(datetime.fromisoformat(stamp)), expected)
        for start, days in (("", 30), ("bad", 30), (None, 30), ("2026-09-04", 0),
                            ("2026-09-04", -1), ("2026-09-04", "bad")):
            with patch.object(config, "THETA_AUDIT_START_DATE", start), patch.object(config, "THETA_AUDIT_DAYS", days):
                self.assertFalse(usage.audit_active())
        self.assertFalse(usage.audit_active(datetime(2026, 9, 4)))

    def test_default_off_missing_config_and_default_duration(self):
        try:
            del config.THETA_AUDIT_START_DATE
            self.sdk.stock_history_ohlc.return_value = pd.DataFrame()
            self.client.stock_history_ohlc(date(2026, 9, 4))
            self.assertEqual(self.records(), [])
            config.THETA_AUDIT_START_DATE = "2026-09-04"
            del config.THETA_AUDIT_DAYS
            self.assertTrue(usage.audit_active())
        finally:
            config.THETA_AUDIT_START_DATE = "2026-09-04"
            config.THETA_AUDIT_DAYS = 30

    def test_completion_stays_in_start_file_past_window_end(self):
        self.clock.return_value = datetime.fromisoformat("2026-10-04T03:59:59+00:00")
        ticket = usage.begin_request("stock_history_ohlc")
        self.clock.return_value = datetime.fromisoformat("2026-10-04T04:01:00+00:00")
        usage.finish_request(ticket, rows=0)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual([p.name for p in self.directory.glob("audit/*")], ["usage-2026-10-03.jsonl"])
        self.assertIsNone(usage.begin_request("stock_history_ohlc"))

    def test_persistence_failure_does_not_change_success_or_error(self):
        self.sdk.stock_history_ohlc.return_value = pd.DataFrame()
        with patch.object(Path, "open", side_effect=OSError("SECRET-PATH")), self.assertLogs(usage.logger, level="WARNING") as logs:
            self.assertEqual(self.client.stock_history_ohlc(date(2026, 9, 4)), [])
            self.sdk.stock_history_ohlc.side_effect = ValueError("SDK-SECRET")
            with self.assertRaises(ThetaDataError):
                self.client.stock_history_ohlc(date(2026, 9, 4))
        self.assertNotIn("SECRET", " ".join(logs.output))

    def test_closed_purpose_and_endpoint_vocabulary(self):
        ticket = usage.begin_request("stock_history_ohlc", "SECRET-LABEL")
        usage.finish_request(ticket, rows=0)
        self.assertEqual(self.records()[0]["purpose"], "unspecified")
        self.assertIsNone(usage.begin_request("SECRET-ENDPOINT"))
        self.assertNotIn("SECRET", json.dumps(self.records()))
        other = ThetaDataClient(client=self.sdk, purpose="SECRET-LABEL")
        self.assertEqual(other._purpose, "unspecified")

    def test_concurrent_calls_have_parseable_unique_pairs(self):
        self.sdk.stock_history_ohlc.return_value = pd.DataFrame()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.client.stock_history_ohlc(date(2026, 9, 4)), range(40)))
        records = self.records()
        self.assertEqual(len(records), 80)
        starts = {r["request_id"] for r in records if r["event"] == "start"}
        finishes = {r["request_id"] for r in records if r["event"] == "finish"}
        self.assertEqual(len(starts), 40)
        self.assertEqual(starts, finishes)

    def test_initialization_failure_is_an_attempt_not_an_unrecorded_gap(self):
        with patch.object(self.client, "_get_client", side_effect=RuntimeError("AUTH-SECRET")):
            with self.assertRaises(ThetaDataError):
                self.client.stock_history_ohlc(date(2026, 9, 4))
        self.assertEqual(len(self.records()), 2)
        self.assertFalse(self.records()[-1]["success"])
        self.assertNotIn("AUTH-SECRET", json.dumps(self.records()))

    def test_raw_result_identity_and_unknown_count_are_preserved(self):
        result = iter([1, 2])
        self.sdk.stock_history_ohlc.return_value = result
        self.assertIs(self.client._request("stock_history_ohlc"), result)
        self.assertIsNone(self.records()[-1]["rows"])
        self.assertEqual(list(result), [1, 2])

    def test_window_uses_calendar_days_across_dst(self):
        with patch.object(config, "THETA_AUDIT_START_DATE", "2026-10-15"):
            self.assertTrue(usage.audit_active(datetime.fromisoformat("2026-11-14T04:59:59+00:00")))
            self.assertFalse(usage.audit_active(datetime.fromisoformat("2026-11-14T05:00:00+00:00")))

    def test_validation_is_not_an_endpoint_call(self):
        with self.assertRaises(ValueError):
            self.client.stock_history_ohlc(date(2026, 9, 4), symbol="SPX")
        self.sdk.stock_history_ohlc.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_only_current_vocabulary_is_admitted(self):
        self.assertEqual(usage._ENDPOINTS, {"stock_history_ohlc": "stock"})
        self.assertEqual(usage.PURPOSES, {"unspecified", "spy_setup"})
        self.assertIsNone(usage.begin_request("index_history_ohlc", "market_state"))
        ticket = usage.begin_request("stock_history_ohlc", "market_state")
        self.assertEqual(ticket.purpose, "unspecified")


if __name__ == "__main__":
    unittest.main()
