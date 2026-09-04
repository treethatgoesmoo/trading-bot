"""Regression coverage for market-data boundaries and calendar rules."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import pytz

from backtest_momentum_extension import grade_forward
from backtest_open_close_momentum import build_daily_sample
from core.analytics import latest_grading_rows
from core.market_calendar import EARLY_CLOSE, close_time
from swing.strategy import completed_daily_candles
from zero_dte import tracker as zero_tracker
from zero_dte.embeds import extension_label, momentum_label
from zero_dte.strategy import _completed_timeframe_direction, compute_30m_forecast

ET = pytz.timezone("US/Eastern")


class CalendarRegressionTests(unittest.TestCase):
    def test_friday_before_sunday_july_fourth_is_early_close(self):
        self.assertEqual(close_time(date(2027, 7, 2)), EARLY_CLOSE)

    def test_thursday_before_saturday_july_fourth_is_early_close(self):
        self.assertEqual(close_time(date(2020, 7, 2)), EARLY_CLOSE)


class GradingSchemaRegressionTests(unittest.TestCase):
    def test_analytics_excludes_older_label_schema(self):
        rows = [
            {"time": "old", "grading_schema": 1},
            {"time": "legacy-without-marker"},
            {"time": "new", "grading_schema": 2},
        ]
        selected, schema = latest_grading_rows(rows)
        self.assertEqual(schema, 2)
        self.assertEqual([r["time"] for r in selected], ["new"])


class CompletedBarRegressionTests(unittest.TestCase):
    def test_daily_helper_drops_current_et_date(self):
        frame = pd.DataFrame({
            "timestamp": [
                ET.localize(datetime(2026, 8, 13)),
                ET.localize(datetime(2026, 8, 14)),
            ],
            "close": [100.0, 999.0],
        })
        result = completed_daily_candles(frame, ET.localize(datetime(2026, 8, 14, 12)))
        self.assertEqual(result["close"].tolist(), [100.0])

    def test_forecast_excludes_current_forming_interval(self):
        # The 10:00 15-minute bar cannot be known at 10:01. Its deliberately
        # extreme close must not become the forecast's current_price.
        timestamps = pd.date_range(
            ET.localize(datetime(2026, 8, 14, 6, 15)), periods=16, freq="15min"
        )
        closes = [100.0 + i for i in range(15)] + [999.0]
        frame = pd.DataFrame({
            "timestamp": timestamps,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        })
        forecast = asyncio.run(compute_30m_forecast(
            None, "SPY", data=frame, interval=15,
            asof=ET.localize(datetime(2026, 8, 14, 10, 1)),
        ))
        self.assertIsNotNone(forecast)
        self.assertEqual(forecast.current_price, 114.0)
        self.assertIsNotNone(forecast.atr)
        self.assertEqual(forecast.candle_30m_direction, "BULLISH")
        self.assertIsNone(forecast.candle_1h_direction)

    def test_completed_higher_timeframes_use_full_candles_only(self):
        timestamps = pd.date_range(
            ET.localize(datetime(2026, 8, 14, 9, 30)), periods=6, freq="15min"
        )
        frame = pd.DataFrame({
            "timestamp": timestamps,
            "open": [100, 101, 102, 103, 104, 105],
            "close": [101, 102, 103, 99, 105, 106],
        })
        # 9:30-10:30 is a complete bearish 1h candle. The two newer bars form
        # a complete bullish 30m candle but not a newer complete 1h candle.
        self.assertEqual(_completed_timeframe_direction(frame, 15, 30), "BULLISH")
        self.assertEqual(_completed_timeframe_direction(frame, 15, 60), "BEARISH")

    def test_plain_english_market_state_labels(self):
        self.assertEqual(momentum_label(-79), "Strong downward")
        self.assertEqual(momentum_label(0), "Flat / weak")
        self.assertEqual(momentum_label(70), "Strong upward")
        self.assertEqual(extension_label(48), "Moderate")
        self.assertIn("reversal risk", extension_label(70))

    def test_momentum_grader_requires_exact_same_session_next_bar(self):
        timestamps = pd.DatetimeIndex([
            ET.localize(datetime(2026, 8, 13, 9, 45)),
            ET.localize(datetime(2026, 8, 13, 10, 0)),
            ET.localize(datetime(2026, 8, 13, 15, 45)),
            ET.localize(datetime(2026, 8, 14, 9, 30)),
        ])
        frame = pd.DataFrame({
            "close": [100.0, 102.0, 103.0, 110.0],
            "atr": [1.0, 1.0, 1.0, 1.0],
            "naive_call": ["BULLISH"] * 4,
            "interval_min": [15] * 4,
        }, index=timestamps)
        result = grade_forward(frame)
        self.assertEqual(result["outcome"].iat[0], "CONTINUATION")
        self.assertTrue(result["outcome"].iloc[1:].isna().all())

    def test_live_grader_prefers_stored_signal_time_atr(self):
        forecast_time = ET.localize(datetime(2026, 8, 14, 10, 0))
        timestamps = pd.date_range(forecast_time, periods=20, freq="15min")
        candles = pd.DataFrame({
            "timestamp": timestamps,
            "open": [100.0] * 20,
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.2] * 20,
            "volume": [1000] * 20,
        })

        class FakeClient:
            async def fetch_candles(self, *args, **kwargs):
                return candles

        entry = {
            "ticker": "SPY",
            "time": forecast_time.astimezone(pytz.UTC).isoformat(),
            "interval": 15,
            "direction": "BULLISH",
            "price": 100.0,
            "atr": 0.1,
        }
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(zero_tracker, "DATA_DIR", tmp), \
                patch.object(zero_tracker, "load_forecasts", return_value=[entry]):
            results = asyncio.run(zero_tracker.evaluate_forecasts(
                FakeClient(), "SPY", "2026-08-14"
            ))
        self.assertEqual(results[0]["deadband_atr_source"], "signal")
        self.assertEqual(results[0]["deadband"], 0.025)
        self.assertEqual(results[0]["actual"], "BULLISH")

    def test_open_close_sample_uses_prior_close_and_final_half_hour(self):
        pieces = []
        for day, base in ((13, 100.0), (14, 110.0)):
            index = pd.date_range(
                ET.localize(datetime(2026, 8, day, 9, 30)), periods=78, freq="5min"
            )
            values = [base + i * 0.1 for i in range(78)]
            pieces.append(pd.DataFrame({
                "open": values,
                "high": [v + 0.2 for v in values],
                "low": [v - 0.2 for v in values],
                "close": [v + 0.05 for v in values],
                "volume": [1000] * 78,
            }, index=index))
        sample = build_daily_sample(pd.concat(pieces))
        self.assertEqual(len(sample), 1)
        prior_close = 100.0 + 77 * 0.1 + 0.05
        opening_close = 110.0 + 5 * 0.1 + 0.05
        final_open = 110.0 + 72 * 0.1
        day_close = 110.0 + 77 * 0.1 + 0.05
        self.assertAlmostEqual(sample.iloc[0]["opening_return"], opening_close / prior_close - 1)
        self.assertAlmostEqual(sample.iloc[0]["final_half_return"], day_close / final_open - 1)

    def test_open_close_sample_does_not_bridge_a_missing_session(self):
        pieces = []
        # Tuesday is intentionally absent. Wednesday must not use Monday's
        # close; Thursday can use Wednesday normally.
        for day, base in ((10, 100.0), (12, 120.0), (13, 130.0)):
            index = pd.date_range(
                ET.localize(datetime(2026, 8, day, 9, 30)), periods=78, freq="5min"
            )
            pieces.append(pd.DataFrame({
                "open": [base] * 78,
                "high": [base + 0.2] * 78,
                "low": [base - 0.2] * 78,
                "close": [base + 0.05] * 78,
                "volume": [1000] * 78,
            }, index=index))
        sample = build_daily_sample(pd.concat(pieces))
        self.assertEqual(sample.index.tolist(), [date(2026, 8, 13)])


if __name__ == "__main__":
    unittest.main()
