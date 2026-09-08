from datetime import date, timedelta
import unittest
import pandas as pd
from zero_dte.orb_audit import session_time
from zero_dte.spx_proxy import summarize_proxy


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.start = session_time(date(2026, 9, 8), 9, 30)
        self.frame = pd.DataFrame({'High': [101.] * 20, 'Low': [99.] * 20,
                                   'Close': [100.] * 20, 'Volume': [100.] * 15 + [200.] * 5},
                                  index=pd.date_range(self.start, periods=20, freq='min'))
        self.now = self.start + timedelta(minutes=20, seconds=10)

    def test_complete_proxy_baseline(self):
        result = summarize_proxy(self.frame, self.now)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['volume_ratio'], 2)
        self.assertEqual(result['source'], 'Yahoo SPY indicative')

    def test_gap_nan_stale_and_insufficient_are_unavailable(self):
        for frame in [self.frame.drop(self.frame.index[5]), self.frame.iloc[:15],
                      self.frame.assign(Volume=float('nan')),
                      self.frame.set_axis(self.frame.index.tz_localize(None))]:
            self.assertNotEqual(summarize_proxy(frame, self.now)['status'], 'available')
        self.assertEqual(summarize_proxy(self.frame, self.now + timedelta(minutes=5))['status'], 'stale')

    def test_partial_minute_not_used(self):
        result = summarize_proxy(self.frame, self.now - timedelta(minutes=1))
        self.assertEqual(result['status'], 'unavailable')
