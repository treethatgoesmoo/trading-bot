"""Regression coverage for market-data boundaries and calendar rules."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import unittest

import pandas as pd
import pytz

from core.market_calendar import EARLY_CLOSE, close_time
from swing.strategy import completed_daily_candles
from zero_dte.strategy import compute_30m_forecast

ET = pytz.timezone("US/Eastern")


class CalendarRegressionTests(unittest.TestCase):
    def test_friday_before_sunday_july_fourth_is_early_close(self):
        self.assertEqual(close_time(date(2027, 7, 2)), EARLY_CLOSE)

    def test_thursday_before_saturday_july_fourth_is_early_close(self):
        self.assertEqual(close_time(date(2020, 7, 2)), EARLY_CLOSE)


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


if __name__ == "__main__":
    unittest.main()
