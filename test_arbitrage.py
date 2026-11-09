import sqlite3
import time
import unittest
from decimal import Decimal as D

from arbitrage import Book, evaluate, fill, scan, setup_db, validate


class ArbitrageTests(unittest.TestCase):
    def test_fill_uses_multiple_levels_and_rejects_insufficient_depth(self):
        rows = [(D("100"), D("1")), (D("102"), D("2"))]
        self.assertEqual(fill(rows, D("2")), D("202"))
        self.assertIsNone(fill(rows, D("4")))

    def test_costs_and_vwap(self):
        now = time.monotonic()
        buy = Book("a", [], [(D("100"), D("1")), (D("102"), D("1"))], now, 0)
        sell = Book("b", [(D("110"), D("2"))], [], now, 0)
        result = evaluate(buy, sell, D("2"), D("0.01"), D("0.01"), D("1"))
        self.assertEqual(result["buy_vwap"], D("101"))
        self.assertEqual(result["gross"], D("18"))
        self.assertEqual(result["fees"], D("4.22"))
        self.assertEqual(result["net"], D("12.78"))

    def test_demo_persists_both_directions(self):
        config = {"base": "BTC", "quote": "USDT", "quantity": "0.01", "exchanges": [
            {"id": "binance", "symbol": "BTCUSDT", "taker_fee": "0"},
            {"id": "kraken", "symbol": "XBTUSDT", "taker_fee": "0"}]}
        db = setup_db(":memory:")
        rows = scan(config, db, demo=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
        self.assertEqual(sum(row[1] for row in rows), 1)
        db.close()

    def test_reject_invalid_fee_and_duplicate_exchange(self):
        config = {"quantity": 1, "exchanges": [
            {"id": "binance", "symbol": "BTCUSDT", "taker_fee": 1},
            {"id": "kraken", "symbol": "XBTUSDT", "taker_fee": 0}]}
        with self.assertRaises(ValueError):
            validate(config)
        config["exchanges"][0]["taker_fee"] = 0
        config["exchanges"][1]["id"] = "binance"
        with self.assertRaises(ValueError):
            validate(config)


if __name__ == "__main__":
    unittest.main()
