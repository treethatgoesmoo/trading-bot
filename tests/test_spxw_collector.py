from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from collect_spxw_research import _safe_data_dir, collect
from core.paths import ROOT
from core.thetadata_client import IndexBar, OptionTradeQuote


SESSION = date(2026, 9, 4)
STAMP = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
AFTER_SESSION = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    def list_expirations(self, symbol):
        return [SESSION]

    def index_history_ohlc(self, *args, **kwargs):
        return [
            IndexBar(datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc), 7700, 7702, 7699, 7701),
            IndexBar(datetime(2026, 9, 4, 19, 59, tzinfo=timezone.utc), 7717, 7719, 7716, 7718),
        ]

    def option_trade_quotes(self, *args, **kwargs):
        return [OptionTradeQuote(
            "SPXW", SESSION, 7700, "call", STAMP, STAMP, 1, 0, 2, 5,
            8.5, 10, 8.0, 12, 8.5,
        )]

    def subscription_summary(self):
        return {"options": "STANDARD", "index": "STANDARD"}


class CollectorTests(unittest.TestCase):
    def test_collection_is_frozen_verified_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("collect_spxw_research.ThetaDataClient", return_value=FakeClient()), patch(
                "collect_spxw_research._spy_rows",
                return_value=[
                    {"timestamp": "2026-09-04T13:30:00+00:00", "open": 600,
                     "high": 601, "low": 599, "close": 600.5, "volume": 1000},
                    {"timestamp": "2026-09-04T19:59:00+00:00", "open": 602,
                     "high": 603, "low": 601, "close": 602.5, "volume": 1200},
                ],
            ):
                destination = collect(
                    SESSION, data_dir=root, strike_range=2, refresh=False, include_spy=True,
                    now=AFTER_SESSION,
                )
                metadata = json.loads((destination / "metadata.json").read_text())
                self.assertEqual(metadata["options_entitlement"], "STANDARD")
                self.assertEqual(metadata["files"]["spxw_trade_quote.csv"]["rows"], 1)

                # Existing data verifies and returns without contacting providers.
                with patch(
                    "collect_spxw_research.ThetaDataClient",
                    side_effect=AssertionError("provider should not be called"),
                ):
                    self.assertEqual(
                        collect(SESSION, data_dir=root, strike_range=2,
                                refresh=False, include_spy=True, now=AFTER_SESSION),
                        destination,
                    )

                with self.assertRaisesRegex(RuntimeError, "parameter mismatch"):
                    collect(SESSION, data_dir=root, strike_range=3,
                            refresh=False, include_spy=True, now=AFTER_SESSION)

                with (destination / "spxw_trade_quote.csv").open("a") as handle:
                    handle.write("tampered\n")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    collect(SESSION, data_dir=root, strike_range=2,
                            refresh=False, include_spy=True, now=AFTER_SESSION)

    def test_rejects_collection_before_current_session_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            midday = datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)  # noon ET
            with self.assertRaisesRegex(RuntimeError, "still in progress"):
                collect(SESSION, data_dir=Path(temporary), strike_range=2,
                        refresh=False, include_spy=False, now=midday)

    def test_rejects_licensed_storage_inside_tracked_repo(self):
        with self.assertRaisesRegex(ValueError, "licensed data path"):
            _safe_data_dir(Path(ROOT) / "data" / "raw-opra")


if __name__ == "__main__":
    unittest.main()
