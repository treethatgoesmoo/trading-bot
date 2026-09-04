from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd

from core.thetadata_client import IndexBar, IndexSnapshot, OptionQuote
from zero_dte.flow_cog import (
    SPXFlowCog, _complete_session_ohlc, _session_ohlc, _spy_vwap_z,
    _valid_exit_quote,
)
from zero_dte.turning_points import FlowSummary, KeyLevel, TurningPointAssessment, TurningState


class FlowCogHelperTests(unittest.TestCase):
    def test_spy_vwap_extension_uses_only_completed_current_session_bars(self):
        frame = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-09-04T13:30:00Z", "2026-09-04T13:31:00Z",
                "2026-09-04T13:32:00Z", "2026-09-04T13:33:00Z",
                "2026-09-04T13:34:00Z", "2026-09-04T13:35:00Z",
                "2026-09-03T19:59:00Z",
            ]),
            "high": [600, 601, 602, 603, 604, 700, 900],
            "low": [599, 600, 601, 602, 603, 699, 899],
            "close": [599.5, 600.5, 601.5, 602.5, 603.5, 699.5, 899.5],
            "volume": [100] * 7,
        })
        # The 09:35 ET bar is still forming at 09:35:30 and must be excluded.
        now = datetime(2026, 9, 4, 13, 35, 30, tzinfo=timezone.utc)
        z_score = _spy_vwap_z(frame, now)
        self.assertIsNotNone(z_score)
        self.assertGreater(z_score, 1.0)
        self.assertLess(z_score, 2.0)

    def test_session_ohlc_aggregates_complete_bar_values(self):
        rows = [
            IndexBar(datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc), 7700, 7705, 7698, 7702),
            IndexBar(datetime(2026, 9, 4, 13, 31, tzinfo=timezone.utc), 7702, 7708, 7699, 7706),
        ]
        result = _session_ohlc(date(2026, 9, 4), rows)
        self.assertEqual((result.open, result.high, result.low, result.close), (7700, 7708, 7698, 7706))
        self.assertIsNone(_complete_session_ohlc(date(2026, 9, 4), rows))

        complete = [
            IndexBar(
                datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=i),
                7700 + i / 100, 7701 + i / 100, 7699 + i / 100, 7700.5 + i / 100,
            )
            for i in range(390)
        ]
        self.assertIsNotNone(_complete_session_ohlc(date(2026, 9, 4), complete))
        self.assertIsNone(_complete_session_ohlc(date(2026, 9, 4), complete[:100] + complete[200:]))

    def test_exit_quote_requires_executable_matching_contract(self):
        target = datetime(2026, 9, 4, 15, 15, tzinfo=timezone.utc)
        contract = {"symbol": "SPXW", "expiration": "2026-09-04", "strike": 7700.0, "right": "put"}
        quote = OptionQuote(
            "SPXW", date(2026, 9, 4), 7700.0, "put", target,
            2, 8.0, 3, 8.5,
        )
        self.assertTrue(_valid_exit_quote(quote, contract, target))
        self.assertFalse(_valid_exit_quote(OptionQuote(**{
            **quote.__dict__, "bid_size": 0
        }), contract, target))
        self.assertFalse(_valid_exit_quote(OptionQuote(**{
            **quote.__dict__, "timestamp": target.replace(tzinfo=None)
        }), contract, target))
        self.assertFalse(_valid_exit_quote(OptionQuote(**{
            **quote.__dict__, "right": "call"
        }), contract, target))


class FlowOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_api_failure_does_not_consume_confirmed_state(self):
        now = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)

        class ThetaFailure:
            def index_snapshots(self, symbols):
                return [IndexSnapshot("SPX", now, 7698.0)]

            def option_trade_quotes(self, *args, **kwargs):
                return []

            def option_chain_snapshot(self, *args, **kwargs):
                raise RuntimeError("temporary chain failure")

        cog = SPXFlowCog(None, None, ThetaFailure())
        cog._session_date = now.date()
        cog._levels = [KeyLevel(7700, "Round level 7700", "round")]
        cog._assessment = TurningPointAssessment(
            TurningState.POTENTIAL_TOP, cog._levels[0], 1.0, 3.2, 0.2, "potential"
        )
        cog._potential_started_at = now - timedelta(minutes=1)
        cog._recent_prices.extend([
            (now - timedelta(minutes=4), 7700.0),
            (now, 7698.0),
        ])
        cog._last_spy_update = now
        cog._vwap_z = 2.0
        with tempfile.TemporaryDirectory() as temporary, patch(
            "zero_dte.flow_cog.THETADATA_DATA_DIR", temporary
        ):
            assessment, _, _, decision = await cog._sample(now)
        self.assertEqual(assessment.state, TurningState.TOP_CONFIRMED)
        self.assertTrue(decision.should_wait)
        self.assertIn("unavailable", decision.reason)

    async def test_posted_shadow_alert_receives_15_and_30_minute_outcomes(self):
        cog = SPXFlowCog(None, None, object())
        entry = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)
        assessment = TurningPointAssessment(
            TurningState.POTENTIAL_TOP,
            KeyLevel(7700, "Round level 7700", "round"),
            2.0, 3.2, 0.3, "paper",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "zero_dte.flow_cog.THETADATA_DATA_DIR", temporary
        ):
            cog._record_posted_alert(entry, 7701.0, assessment, None)
            await cog._grade_pending_alerts(entry + timedelta(minutes=15), 7698.0)
            await cog._grade_pending_alerts(entry + timedelta(minutes=30), 7702.0)
            rows = cog._load_alerts(entry.date())
        self.assertTrue(rows[0]["outcomes"]["15"]["direction_correct"])
        self.assertFalse(rows[0]["outcomes"]["30"]["direction_correct"])
        self.assertEqual(rows[0]["outcomes"]["15"]["spx_move"], -3.0)


if __name__ == "__main__":
    unittest.main()
