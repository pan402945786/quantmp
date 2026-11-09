import sqlite3
import tempfile
import unittest
from pathlib import Path

from research_system import backtest, load_price_csv, run_experiment


class ResearchSystemTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent
        self.data = self.root / "data" / "demo_prices.csv"
        self.config = {"initial_capital": 100000, "lookback_days": 5, "max_weight": 0.6,
                       "transaction_cost_bps": 10, "strategy_version": "test-v1"}

    def test_loader_is_rectangular_and_reproducible(self):
        first = load_price_csv(self.data)
        second = load_price_csv(self.data)
        self.assertEqual(first.dates, second.dates)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.assets, ("AAA", "BBB", "CCC"))

    def test_momentum_uses_delayed_execution_and_costs(self):
        panel = load_price_csv(self.data)
        result = backtest(panel, self.config, "momentum")
        self.assertEqual(result.predictions[0]["decision_date"], "2025-01-06")
        self.assertEqual(result.predictions[0]["execution_date"], "2025-01-07")
        self.assertGreater(len(result.trades), 0)
        self.assertLess(result.metrics["ending_equity"], 100000 * (111 / 100))
        self.assertGreaterEqual(result.metrics["max_drawdown"], -1.0)

    def test_relative_reversion_is_price_only_and_traceable(self):
        panel = load_price_csv(self.data)
        result = backtest(panel, self.config, "relative_reversion")
        self.assertEqual(result.predictions[0]["decision_date"], "2025-01-06")
        self.assertIn("BBB", result.predictions[0]["targets"])
        self.assertGreater(len(result.trades), 0)
        self.assertLessEqual(max(result.predictions[0]["targets"].values()), 0.6)

    def test_database_contains_traceable_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "research.sqlite3"
            experiment, results = run_experiment(self.data, self.config, ["momentum", "relative_reversion", "cash"], database)
            self.assertEqual(len(experiment), 16)
            self.assertEqual(len(results), 3)
            db = sqlite3.connect(database)
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 1)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM portfolio_daily").fetchone()[0], 0)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0], 0)
            finally:
                db.close()

    def test_rejects_future_or_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("date,asset,close\n2025-01-01,AAA,1\n2025-01-02,BBB,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_price_csv(path)


if __name__ == "__main__":
    unittest.main()
