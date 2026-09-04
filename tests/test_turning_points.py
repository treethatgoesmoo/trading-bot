from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from core.thetadata_client import OptionSnapshot, OptionTradeQuote
from zero_dte.turning_points import (
    Aggressor,
    SessionOHLC,
    build_key_levels,
    infer_aggressor,
    select_contract,
    summarize_option_flow,
)


NOW = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)


def trade(*, right="call", price=2.0, bid=1.9, ask=2.0, quote_age=0.5):
    return OptionTradeQuote(
        symbol="SPXW", expiration=NOW.date(), strike=7700.0, right=right,
        trade_timestamp=NOW, quote_timestamp=NOW - timedelta(seconds=quote_age),
        sequence=1, condition=0, size=2, exchange=5, price=price,
        bid_size=10, bid=bid, ask_size=10, ask=ask,
    )


def option(*, right="call", ask=8.5, bid=8.0, delta=0.5, strike=7700.0,
           quote_age=1.0, expiration=None):
    mid = (ask + bid) / 2
    return OptionSnapshot(
        symbol="SPXW", expiration=expiration or NOW.date(), strike=strike, right=right,
        quote_timestamp=NOW - timedelta(seconds=quote_age), bid=bid, ask=ask,
        bid_size=10, ask_size=10, mid=mid,
        spread_pct=(ask - bid) / mid * 100, delta=delta,
        greeks_timestamp=NOW - timedelta(seconds=quote_age),
    )


class OptionFlowTests(unittest.TestCase):
    def test_midpoint_rule_and_quote_quality_rejections(self):
        self.assertEqual(infer_aggressor(trade(price=2.0)), Aggressor.ASK)
        self.assertEqual(infer_aggressor(trade(price=1.9)), Aggressor.BID)
        self.assertEqual(infer_aggressor(trade(price=1.95)), Aggressor.MID)
        self.assertEqual(infer_aggressor(trade(price=2.0, bid=2.1, ask=2.0)), Aggressor.INVALID)
        self.assertEqual(infer_aggressor(trade(price=2.0, quote_age=3.0)), Aggressor.INVALID)
        aware = trade(price=2.0)
        self.assertEqual(
            infer_aggressor(OptionTradeQuote(**{
                **aware.__dict__, "trade_timestamp": aware.trade_timestamp.replace(tzinfo=None)
            })),
            Aggressor.INVALID,
        )
        self.assertEqual(
            infer_aggressor(OptionTradeQuote(**{
                **aware.__dict__, "quote_timestamp": aware.quote_timestamp.replace(tzinfo=None)
            })),
            Aggressor.INVALID,
        )

    def test_flow_summary_uses_call_put_and_aggressor_direction(self):
        rows = [
            trade(right="call", price=2.0),                 # +$400
            trade(right="call", price=1.9),                 # -$380
            trade(right="put", price=2.0),                  # -$400
            trade(right="put", price=1.9),                  # +$380
        ]
        summary = summarize_option_flow(rows)
        self.assertEqual(summary.classified_trades, 4)
        self.assertAlmostEqual(summary.net_directional_premium, 0.0)
        self.assertEqual(summary.classified_pct, 100.0)


class ContractSelectionTests(unittest.TestCase):
    def test_selects_liquid_delta_nearest_target_under_budget(self):
        choices = [
            option(delta=0.35, strike=7710.0, ask=5.0, bid=4.7),
            option(delta=0.49, strike=7700.0, ask=9.0, bid=8.5),
            option(delta=0.55, strike=7695.0, ask=11.0, bid=10.5),
        ]
        decision = select_contract(
            choices, direction="call", spx_price=7701.0, now=NOW,
            market_close=NOW + timedelta(hours=1),
        )
        self.assertFalse(decision.should_wait)
        self.assertEqual(decision.contract.strike, 7700.0)

    def test_waits_instead_of_buying_lottery_contract(self):
        decision = select_contract(
            [option(delta=0.08, ask=2.0, bid=1.9)], direction="call",
            spx_price=7700.0, now=NOW, market_close=NOW + timedelta(hours=1),
        )
        self.assertTrue(decision.should_wait)
        self.assertIn("low/high-delta", decision.reason)

    def test_uses_eastern_session_date_near_utc_midnight(self):
        late_utc = datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)
        candidate = option(expiration=date(2026, 9, 4))
        candidate = OptionSnapshot(**{
            **candidate.__dict__,
            "quote_timestamp": late_utc - timedelta(seconds=1),
            "greeks_timestamp": late_utc - timedelta(seconds=1),
        })
        decision = select_contract(
            [candidate], direction="call", spx_price=7700,
            now=late_utc, market_close=late_utc + timedelta(hours=1),
        )
        self.assertFalse(decision.should_wait)

    def test_waits_for_budget_staleness_time_and_unconfirmed_setup(self):
        common = dict(spx_price=7700.0, now=NOW, market_close=NOW + timedelta(hours=1))
        self.assertTrue(select_contract([option(ask=12, bid=11.5)], direction="call", **common).should_wait)
        self.assertTrue(select_contract([option(quote_age=20)], direction="call", **common).should_wait)
        self.assertTrue(select_contract(
            [option()], direction="call", spx_price=7700.0, now=NOW,
            market_close=NOW + timedelta(minutes=20),
        ).should_wait)
        self.assertTrue(select_contract([option()], direction="wait", **common).should_wait)


class KeyLevelTests(unittest.TestCase):
    def test_levels_use_only_prior_sessions(self):
        rows = [
            SessionOHLC(date(2026, 8, 31), 7600, 7620, 7590, 7610),
            SessionOHLC(date(2026, 9, 1), 7620, 7650, 7610, 7640),
            SessionOHLC(date(2026, 9, 2), 7640, 7690, 7630, 7680),
            SessionOHLC(date(2026, 9, 3), 7680, 7720, 7670, 7710),
            SessionOHLC(date(2026, 9, 5), 9000, 9100, 8900, 9050),  # future: reject
        ]
        levels = build_key_levels(
            rows, session_date=date(2026, 9, 4), session_open=7712,
            reference_price=7718,
        )
        by_label = {level.label: level.price for level in levels}
        self.assertEqual(by_label["Previous day high"], 7720)
        self.assertEqual(by_label["Current week open"], 7600)
        self.assertEqual(by_label["Current month open"], 7620)
        self.assertNotIn(9100, [level.price for level in levels])
        self.assertIn(7725, [level.price for level in levels if level.kind == "round"])

    def test_missing_immediately_prior_session_does_not_bridge_daily_level(self):
        levels = build_key_levels(
            [SessionOHLC(date(2026, 9, 2), 7640, 7690, 7630, 7680)],
            session_date=date(2026, 9, 4), session_open=7712,
            reference_price=7718,
        )
        self.assertNotIn("Previous day close", {level.label for level in levels})
        self.assertNotIn("Current week open", {level.label for level in levels})


if __name__ == "__main__":
    unittest.main()
