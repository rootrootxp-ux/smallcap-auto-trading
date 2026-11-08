import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from collect import INDEXES, STOCKS, SYMBOLS, inspect_prices


class PriceProbeTests(unittest.TestCase):
    def setUp(self):
        dates = pd.DatetimeIndex(["2026-11-05", "2026-11-06"], tz="Asia/Tokyo")
        self.frame = pd.DataFrame(
            {(symbol, "Close"): [100.0, 101.0] for symbol in SYMBOLS},
            index=dates,
        )
        self.frame.columns = pd.MultiIndex.from_tuples(self.frame.columns)
        self.now = datetime(2026, 11, 9, 7, 30, tzinfo=ZoneInfo("Asia/Tokyo"))

    def test_all_15_stocks_and_two_indices(self):
        rows, errors = inspect_prices(self.frame, self.now)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 17)
        self.assertEqual(len(STOCKS), 15)
        self.assertEqual(len(INDEXES), 2)
        self.assertEqual(rows[0]["change_pct"], 1.0)
        self.assertEqual(rows[0]["date"], "2026-11-06")

    def test_missing_close_causes_failure(self):
        rows, errors = inspect_prices(self.frame.drop(columns=(STOCKS[0], "Close")), self.now)
        self.assertEqual(len(rows), 16)
        self.assertTrue(any(STOCKS[0] in e for e in errors))

    def test_mismatched_date_causes_failure(self):
        changed = self.frame.copy()
        changed.loc[changed.index[-1], (STOCKS[1], "Close")] = float("nan")
        _, errors = inspect_prices(changed, self.now)
        self.assertTrue(errors)

    def test_stale_or_future_data_causes_failure(self):
        old = self.frame.copy()
        old.index = old.index - pd.Timedelta(days=10)
        _, errors = inspect_prices(old, self.now)
        self.assertTrue(any("stale" in e for e in errors))

    def test_field_first_layout(self):
        inverse = self.frame.copy()
        inverse.columns = inverse.columns.swaplevel(0, 1)
        rows, errors = inspect_prices(inverse, self.now)
        self.assertEqual(len(rows), 17)
        self.assertFalse(errors)


if __name__ == "__main__":
    unittest.main()
