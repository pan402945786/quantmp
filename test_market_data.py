import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_data import fetch_daily_closes, write_price_panel


class MarketDataTests(unittest.TestCase):
    def test_fetch_parses_daily_close_and_deduplicates_pages(self):
        payload = [[1609459200000, "1", "2", "0.5", "1.5", "10", 0],
                   [1609545600000, "1.5", "2", "1", "1.75", "10", 0]]
        with patch("market_data.request_klines", return_value=payload):
            rows = fetch_daily_closes("SOLUSDT", "2021-01-01", "2021-01-03")
        self.assertEqual(rows, [("2021-01-01", "SOLUSDT", "1.5"),
                                ("2021-01-02", "SOLUSDT", "1.75")])

    def test_writer_rejects_non_rectangular_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                write_price_panel([("2021-01-01", "BTCUSDT", "1"),
                                   ("2021-01-01", "SOLUSDT", "2"),
                                   ("2021-01-02", "BTCUSDT", "1.1")],
                                  Path(directory) / "prices.csv")

    def test_writer_creates_hashable_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = write_price_panel([("2021-01-02", "SOLUSDT", "2"),
                                          ("2021-01-01", "SOLUSDT", "1")],
                                         Path(directory) / "prices.csv")
            self.assertEqual(metadata["rows"], 2)
            self.assertEqual(len(metadata["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
