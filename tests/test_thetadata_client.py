from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

import pandas as pd

from core.thetadata_client import (
    ThetaDataClient,
    ThetaDataConfigurationError,
)


class FakeThetaClient:
    options_subscription = "STANDARD"
    index_subscription = "STANDARD"
    stock_subscription = "FREE"

    def option_list_expirations(self, symbol):
        return pd.DataFrame({
            "symbol": [symbol, symbol, symbol],
            "expiration": [date(2026, 9, 4), date(2026, 9, 5), date(2026, 9, 4)],
        })

    def index_snapshot_price(self, symbol):
        return pd.DataFrame({
            "timestamp": ["2026-09-04T14:00:00Z", "2026-09-04T14:00:00Z"],
            "symbol": symbol,
            "price": [7700.25, 16.5],
        })

    def option_snapshot_quote(self, **kwargs):
        return pd.DataFrame({
            "timestamp": ["2026-09-04T14:00:00Z", "2026-09-04T14:00:00Z"],
            "symbol": ["SPX", "SPX"],
            "expiration": [kwargs["expiration"], kwargs["expiration"]],
            "strike": [7700.0, 7700.0],
            "right": ["call", "put"],
            "bid_size": [10, 8],
            "bid": [8.0, 7.5],
            "ask_size": [12, 9],
            "ask": [8.5, 8.0],
        })

    def option_snapshot_greeks_first_order(self, **kwargs):
        return pd.DataFrame({
            "symbol": ["SPX", "SPX"],
            "expiration": [kwargs["expiration"], kwargs["expiration"]],
            "strike": [7700.0, 7700.0],
            "right": ["call", "put"],
            "delta": [0.51, -0.49],
            "theta": [-1.2, -1.1],
            "vega": [0.4, 0.4],
            "rho": [0.02, -0.02],
            "implied_vol": [0.22, 0.24],
            "underlying_timestamp": ["2026-09-04T14:00:00Z"] * 2,
            "underlying_price": [7700.25] * 2,
        })

    def option_snapshot_open_interest(self, **kwargs):
        return pd.DataFrame({
            "symbol": ["SPX", "SPX"],
            "expiration": [kwargs["expiration"], kwargs["expiration"]],
            "strike": [7700.0, 7700.0],
            "right": ["call", "put"],
            "open_interest": [1000, 1200],
        })

    def option_snapshot_ohlc(self, **kwargs):
        return pd.DataFrame({
            "symbol": ["SPX", "SPX"],
            "expiration": [kwargs["expiration"], kwargs["expiration"]],
            "strike": [7700.0, 7700.0],
            "right": ["call", "put"],
            "volume": [250, 300],
        })


class ThetaDataClientTests(unittest.TestCase):
    def test_requires_api_key_without_injected_client(self):
        client = ThetaDataClient(api_key="")
        with self.assertRaises(ThetaDataConfigurationError):
            client.subscription_summary()

    def test_reports_entitlements_and_normalizes_index_prices(self):
        client = ThetaDataClient(client=FakeThetaClient())
        self.assertEqual(client.subscription_summary()["options"], "STANDARD")
        snapshots = client.index_snapshots(("SPX", "VIX"))
        self.assertEqual([item.symbol for item in snapshots], ["SPX", "VIX"])
        self.assertEqual(snapshots[0].price, 7700.25)

    def test_expirations_are_sorted_and_deduplicated(self):
        client = ThetaDataClient(client=FakeThetaClient())
        self.assertEqual(
            client.list_expirations("SPX"),
            [date(2026, 9, 4), date(2026, 9, 5)],
        )

    def test_option_snapshot_uses_nbbo_and_merges_standard_details(self):
        client = ThetaDataClient(client=FakeThetaClient())
        snapshots = client.option_chain_snapshot(
            date(2026, 9, 4), symbol="SPX", strike_range=2
        )
        self.assertEqual(len(snapshots), 2)
        call = snapshots[0]
        self.assertEqual(call.right, "call")
        self.assertEqual(call.mid, 8.25)
        self.assertEqual(call.spread_pct, 6.06)
        self.assertEqual(call.delta, 0.51)
        self.assertEqual(call.open_interest, 1000)
        self.assertEqual(call.volume, 250)
        self.assertEqual(call.underlying_price, 7700.25)
        age = call.quote_age_seconds(datetime(2026, 9, 4, 14, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(age, 5.0)

    def test_optional_detail_failure_keeps_executable_quotes(self):
        fake = FakeThetaClient()
        fake.option_snapshot_greeks_first_order = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("temporary failure")
        )
        client = ThetaDataClient(client=fake)
        snapshots = client.option_chain_snapshot(date(2026, 9, 4))
        self.assertEqual(len(snapshots), 2)
        self.assertIsNone(snapshots[0].delta)
        self.assertEqual(snapshots[0].bid, 8.0)


if __name__ == "__main__":
    unittest.main()
