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
        self.client = ThetaDataClient(api_key="PRIVATE-KEY", client=self.sdk, purpose="spx_flow")

    def records(self):
        return [json.loads(line) for path in sorted(self.directory.glob("audit/*.jsonl"))
                for line in path.read_text().splitlines()]

    def test_pair_raw_count_before_normalization_and_redaction(self):
        self.sdk.option_list_expirations.return_value = pd.DataFrame({
            "expiration": ["2026-09-04", "2026-09-04", "invalid"],
            "private_payload": ["SECRET-RESPONSE"] * 3,
        })
        self.assertEqual(self.client.list_expirations("SECRET-SYMBOL"), [date(2026, 9, 4)])
        start, finish = self.records()
        self.assertEqual(start["event"], "start")
        self.assertEqual(finish["event"], "finish")
        self.assertEqual(start["request_id"], finish["request_id"])
        self.assertEqual(finish["rows"], 3)
        self.assertTrue(finish["success"])
        self.assertIsNone(finish["error_class"])
        self.assertEqual(finish["product"], "options")
        self.assertEqual(finish["purpose"], "spx_flow")
        self.assertGreaterEqual(finish["elapsed_ms"], 0)
        self.assertTrue(start["started_at"].endswith("+00:00"))
        text = json.dumps(self.records())
        for secret in ("PRIVATE-KEY", "SECRET-RESPONSE", "SECRET-SYMBOL", "private_payload"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(finish), {
            "schema", "event", "request_id", "endpoint", "product", "purpose",
            "started_at", "finished_at", "elapsed_ms", "success", "error_class", "rows",
        })

    def test_sdk_error_preserves_exception_and_records_class_only(self):
        failure = RuntimeError("secret token and raw response")
        self.sdk.index_snapshot_price.side_effect = failure
        with self.assertRaises(ThetaDataError) as caught:
            self.client.index_snapshots()
        self.assertIs(caught.exception.__cause__, failure)
        finish = self.records()[-1]
        self.assertFalse(finish["success"])
        self.assertEqual(finish["error_class"], "RuntimeError")
        self.assertIsNone(finish["rows"])
        self.assertEqual(finish["product"], "index")
        self.assertNotIn("secret token", json.dumps(self.records()))

    def test_start_is_persisted_before_sdk_runs(self):
        def during_call(**kwargs):
            self.assertEqual([r["event"] for r in self.records()], ["start"])
            return pd.DataFrame()
        self.sdk.index_snapshot_price.side_effect = during_call
        self.client.index_snapshots()
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
            self.sdk.index_snapshot_price.return_value = pd.DataFrame()
            self.client.index_snapshots()
            self.assertEqual(self.records(), [])
            config.THETA_AUDIT_START_DATE = "2026-09-04"
            del config.THETA_AUDIT_DAYS
            self.assertTrue(usage.audit_active())
        finally:
            config.THETA_AUDIT_START_DATE = "2026-09-04"
            config.THETA_AUDIT_DAYS = 30

    def test_completion_stays_in_start_file_past_window_end(self):
        self.clock.return_value = datetime.fromisoformat("2026-10-04T03:59:59+00:00")
        ticket = usage.begin_request("index_snapshot_price")
        self.clock.return_value = datetime.fromisoformat("2026-10-04T04:01:00+00:00")
        usage.finish_request(ticket, rows=0)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual([p.name for p in self.directory.glob("audit/*")], ["usage-2026-10-03.jsonl"])
        self.assertIsNone(usage.begin_request("index_snapshot_price"))

    def test_persistence_failure_does_not_change_success_or_error(self):
        self.sdk.index_snapshot_price.return_value = pd.DataFrame()
        with patch.object(Path, "open", side_effect=OSError("SECRET-PATH")), self.assertLogs(usage.logger, level="WARNING") as logs:
            self.assertEqual(self.client.index_snapshots(), [])
            self.sdk.index_snapshot_price.side_effect = ValueError("SDK-SECRET")
            with self.assertRaises(ThetaDataError):
                self.client.index_snapshots()
        self.assertNotIn("SECRET", " ".join(logs.output))

    def test_closed_purpose_and_endpoint_vocabulary(self):
        ticket = usage.begin_request("option_snapshot_quote", "SECRET-LABEL")
        usage.finish_request(ticket, rows=0)
        self.assertEqual(self.records()[0]["purpose"], "unspecified")
        self.assertIsNone(usage.begin_request("SECRET-ENDPOINT"))
        self.assertNotIn("SECRET", json.dumps(self.records()))
        other = ThetaDataClient(client=self.sdk, purpose="SECRET-LABEL")
        self.assertEqual(other._purpose, "unspecified")

    def test_all_endpoints_and_optional_failure_are_individually_counted(self):
        for endpoint in usage._ENDPOINTS:
            getattr(self.sdk, endpoint).return_value = pd.DataFrame()
        self.sdk.option_snapshot_quote.return_value = pd.DataFrame({
            "symbol": ["SPXW"], "expiration": [date(2026, 9, 4)], "strike": [6000],
            "right": ["call"], "bid": [10], "ask": [11],
        })
        self.sdk.option_snapshot_open_interest.side_effect = RuntimeError("auxiliary unavailable")
        self.client.list_expirations()
        self.client.index_snapshots()
        self.client.index_history_ohlc(date(2026, 9, 4))
        self.client.option_trade_quotes(date(2026, 9, 4))
        self.client.option_quote_history(date(2026, 9, 4), expiration=date(2026, 9, 4), strike=6000, right="call")
        with self.assertLogs("core.thetadata_client", level="WARNING"):
            chain = self.client.option_chain_snapshot(date(2026, 9, 4))
        self.assertEqual(len(chain), 1)
        finishes = [r for r in self.records() if r["event"] == "finish"]
        self.assertEqual(len(finishes), 9)
        self.assertEqual({r["endpoint"] for r in finishes}, set(usage._ENDPOINTS))
        failures = [r for r in finishes if not r["success"]]
        self.assertEqual([r["endpoint"] for r in failures], ["option_snapshot_open_interest"])

    def test_no_detail_calls_when_disabled_or_quotes_empty(self):
        self.sdk.option_snapshot_quote.return_value = pd.DataFrame({"bid": [1]})
        self.client.option_chain_snapshot(date(2026, 9, 4), include_details=False)
        self.sdk.option_snapshot_quote.return_value = pd.DataFrame()
        self.client.option_chain_snapshot(date(2026, 9, 4))
        self.assertEqual([r["endpoint"] for r in self.records() if r["event"] == "start"],
                         ["option_snapshot_quote"] * 2)
        self.sdk.option_snapshot_greeks_first_order.assert_not_called()

    def test_concurrent_calls_have_parseable_unique_pairs(self):
        self.sdk.index_snapshot_price.return_value = pd.DataFrame()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.client.index_snapshots(), range(40)))
        records = self.records()
        self.assertEqual(len(records), 80)
        starts = {r["request_id"] for r in records if r["event"] == "start"}
        finishes = {r["request_id"] for r in records if r["event"] == "finish"}
        self.assertEqual(len(starts), 40)
        self.assertEqual(starts, finishes)

    def test_initialization_failure_is_an_attempt_not_an_unrecorded_gap(self):
        with patch.object(self.client, "_get_client", side_effect=RuntimeError("AUTH-SECRET")):
            with self.assertRaises(ThetaDataError):
                self.client.index_snapshots()
        self.assertEqual(len(self.records()), 2)
        self.assertFalse(self.records()[-1]["success"])
        self.assertNotIn("AUTH-SECRET", json.dumps(self.records()))

    def test_raw_result_identity_and_unknown_count_are_preserved(self):
        result = iter([1, 2])
        self.sdk.index_snapshot_price.return_value = result
        self.assertIs(self.client._request("index_snapshot_price"), result)
        self.assertIsNone(self.records()[-1]["rows"])
        self.assertEqual(list(result), [1, 2])

    def test_window_uses_calendar_days_across_dst(self):
        with patch.object(config, "THETA_AUDIT_START_DATE", "2026-10-15"):
            self.assertTrue(usage.audit_active(datetime.fromisoformat("2026-11-14T04:59:59+00:00")))
            self.assertFalse(usage.audit_active(datetime.fromisoformat("2026-11-14T05:00:00+00:00")))

    def test_validation_and_subscription_summary_are_not_endpoint_calls(self):
        with self.assertRaises(ValueError):
            self.client.option_chain_snapshot(date(2026, 9, 4), right="bad")
        self.sdk.options_subscription = "STANDARD"
        self.sdk.index_subscription = "STANDARD"
        self.sdk.stock_subscription = "FREE"
        self.client.subscription_summary()
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
