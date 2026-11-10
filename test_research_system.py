import math
import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from research_system import (
    PricePanel, STRATEGIES, _rebalance, backtest, load_price_csv, run_experiment,
    save_experiment, setup_research_db,
)


class ResearchSystemTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent
        self.data = self.root / "data" / "demo_prices.csv"
        self.config = {"initial_capital": 100000, "lookback_days": 5, "max_weight": 0.6,
                       "transaction_cost_bps": 10, "strategy_version": "test-v1"}

    def panel(self, rows):
        dates = tuple(f"2025-01-{index:02d}" for index in range(1, len(rows) + 1))
        assets = tuple(sorted(rows[0]))
        return PricePanel(dates, assets, dict(zip(dates, rows)), "test", "test")

    def constant_panel(self):
        return self.panel([{"AAA": 10.0, "BBB": 20.0} for _ in range(4)])

    def test_full_investment_reserves_fees_and_initial_equity(self):
        config = {**self.config, "initial_capital": 100, "lookback_days": 1,
                  "max_weight": 1, "transaction_cost_bps": 100}
        result = backtest(self.constant_panel(), config, "equal_weight")
        expected = 100 / 1.01
        self.assertEqual(result.portfolio[0], {
            "date": "2025-01-02", "as_of": "2025-01-03T00:00:00+00:00", "equity": 100, "cash": 100,
            "weights": {"AAA": 0.0, "BBB": 0.0}, "traded": False,
        })
        self.assertAlmostEqual(result.portfolio[1]["equity"], expected)
        self.assertAlmostEqual(result.portfolio[1]["cash"], 0)
        self.assertAlmostEqual(sum(trade["fee"] for trade in result.trades), 100 - expected)
        self.assertAlmostEqual(result.metrics["ending_equity"], expected)
        self.assertAlmostEqual(result.metrics["max_drawdown"], expected / 100 - 1)
        self.assertAlmostEqual(result.metrics["volatility"], (1 - expected / 100) * math.sqrt(365 / 2))
        self.assertAlmostEqual(result.metrics["annualized_return"], (expected / 100) ** (365 / 2) - 1)
        for row in result.portfolio[1:]:
            self.assertGreaterEqual(row["cash"], 0)
            self.assertAlmostEqual(row["weights"]["AAA"], 0.5)
            self.assertAlmostEqual(sum(row["weights"].values()) + row["cash"] / row["equity"], 1)

    def test_post_fee_cap_preserves_cash_budget(self):
        config = {**self.config, "initial_capital": 100, "lookback_days": 1,
                  "transaction_cost_bps": 100}
        with patch.dict(STRATEGIES, {"single": lambda *args: {"AAA": 0.6}}):
            result = backtest(self.constant_panel(), config, "single")
        row = result.portfolio[1]
        self.assertAlmostEqual(row["equity"], 100 / 1.006)
        self.assertLessEqual(row["weights"]["AAA"], 0.6 + 1e-14)
        self.assertAlmostEqual(row["cash"] / row["equity"], 0.4)

    def test_rebalance_handles_extreme_scale_and_fee_without_losing_budget(self):
        values, _, cash, _ = _rebalance({"AAA": 0.0}, 1e308, {"AAA": 1.0}, 0.01)
        self.assertTrue(math.isfinite(values["AAA"]))
        self.assertTrue(math.isclose(values["AAA"] / 1e308, 1 / 1.01, rel_tol=1e-14))
        self.assertEqual(cash, 0)
        rate = math.nextafter(1.0, 0.0)
        values, _, cash, _ = _rebalance({"AAA": 100.0, "BBB": 0.0}, 0, {"BBB": 1.0}, rate)
        with localcontext() as context:
            context.prec = 60
            decimal_rate = Decimal.from_float(rate)
            expected = float(Decimal(100) * (1 - decimal_rate) / (1 + decimal_rate))
        self.assertTrue(math.isclose(values["BBB"], expected, rel_tol=1e-14))
        self.assertEqual(cash, 0)

    def test_unchanged_holdings_generate_no_trades(self):
        for rate in [0, 0.001, 0.5]:
            with self.subTest(rate=rate):
                values, trades, cash, fee = _rebalance({"AAA": 100.0}, 0, {"AAA": 1.0}, rate)
                self.assertEqual(values, {"AAA": 100.0})
                self.assertEqual(trades, {"AAA": 0.0})
                self.assertEqual((cash, fee), (0, 0))

    def test_fees_can_turn_an_unchanged_pre_cost_target_into_a_sale(self):
        values, trades, cash, fee = _rebalance({"AAA": 60.0, "BBB": 20.0}, 20, {"AAA": 0.6, "BBB": 0.3}, 0.01)
        expected_equity = 99.6 / 0.997
        self.assertLess(trades["AAA"], 0)
        self.assertGreater(trades["BBB"], 0)
        self.assertAlmostEqual(sum(values.values()) + cash, expected_equity)
        self.assertAlmostEqual(expected_equity + fee, 100)

    def test_one_execution_includes_fee_in_metrics(self):
        config = {**self.config, "initial_capital": 100, "lookback_days": 1,
                  "max_weight": 1, "transaction_cost_bps": 100}
        panel = self.panel([{"AAA": 10.0} for _ in range(3)])
        result = backtest(panel, config, "equal_weight")
        self.assertEqual(len(result.portfolio), 2)
        self.assertAlmostEqual(result.metrics["max_drawdown"], 1 / 1.01 - 1)
        self.assertAlmostEqual(result.metrics["annualized_return"], (1 / 1.01) ** 365 - 1)
        self.assertEqual(result.metrics["volatility"], 0)
        self.assertEqual(result.metrics["rebalances"], 1)

    def test_rotation_and_liquidation_charge_both_sides(self):
        current, _, cash, fee = _rebalance({"AAA": 0.0, "BBB": 0.0}, 100, {"AAA": 1.0}, 0.01)
        self.assertAlmostEqual(current["AAA"], 100 / 1.01)
        rotated, trades, cash_after, rotation_fee = _rebalance(current, cash, {"BBB": 1.0}, 0.01)
        expected = (100 / 1.01) * 0.99 / 1.01
        self.assertAlmostEqual(rotated["BBB"], expected)
        self.assertAlmostEqual(rotation_fee, (current["AAA"] + expected) * 0.01)
        self.assertLess(trades["AAA"], 0)
        self.assertGreater(trades["BBB"], 0)
        self.assertGreaterEqual(cash_after, 0)
        liquidated, _, final_cash, exit_fee = _rebalance(rotated, cash_after, {}, 0.01)
        self.assertEqual(sum(liquidated.values()), 0)
        self.assertAlmostEqual(final_cash, expected * 0.99)
        self.assertAlmostEqual(final_cash + fee + rotation_fee + exit_fee, 100)

    def test_cash_and_zero_cost_baselines_have_known_results(self):
        panel = self.panel([{"AAA": 10, "BBB": 10}, {"AAA": 10, "BBB": 10},
                            {"AAA": 20, "BBB": 10}, {"AAA": 40, "BBB": 10}])
        config = {**self.config, "initial_capital": 100, "lookback_days": 1,
                  "transaction_cost_bps": 0}
        cash = backtest(panel, config, "cash")
        self.assertEqual(cash.trades, ())
        self.assertEqual(cash.metrics["ending_equity"], 100)
        self.assertEqual(cash.metrics["average_cash_weight"], 1)
        self.assertTrue(all(value == 0 for key, value in cash.metrics.items()
                            if key not in {"ending_equity", "average_cash_weight"}))
        equal = backtest(panel, config, "equal_weight")
        self.assertAlmostEqual(equal.metrics["ending_equity"], 150)
        self.assertTrue(all(trade["fee"] == 0 for trade in equal.trades))

    def test_daily_ledger_reconciles_to_recorded_trades(self):
        panel = load_price_csv(self.data)
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                result = backtest(panel, self.config, strategy)
                cash = self.config["initial_capital"]
                units = dict.fromkeys(panel.assets, 0.0)
                for row in result.portfolio:
                    for trade in (item for item in result.trades if item["date"] == row["date"]):
                        signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
                        units[trade["asset"]] += signed / trade["price"]
                        cash -= signed + trade["fee"]
                        self.assertAlmostEqual(trade["fee"], trade["notional"] * 0.001)
                    values = {asset: units[asset] * panel.price(row["date"], asset) for asset in panel.assets}
                    self.assertAlmostEqual(cash, row["cash"], places=7)
                    self.assertAlmostEqual(cash + sum(values.values()), row["equity"], places=7)
                    self.assertGreaterEqual(row["cash"], 0)
                    for asset in panel.assets:
                        self.assertAlmostEqual(values[asset] / row["equity"], row["weights"][asset])
                        self.assertLessEqual(row["weights"][asset], 0.6 + 1e-12)

    def test_rebalance_conserves_funds_across_prices_and_budgets(self):
        rng = random.Random(20261110)
        cash = 100000.0
        values = dict.fromkeys(("AAA", "BBB", "CCC"), 0.0)
        for _ in range(250):
            values = {asset: value * rng.uniform(0.3, 3) for asset, value in values.items()}
            targets = {asset: rng.random() for asset in values}
            scale = rng.random() / sum(targets.values())
            targets = {asset: weight * scale for asset, weight in targets.items()}
            rate = rng.choice([0, 0.001, 0.02, 0.5, 0.9999])
            before = cash + sum(values.values())
            after_values, trades, cash, fee = _rebalance(values, cash, targets, rate)
            after = cash + sum(after_values.values())
            self.assertGreaterEqual(cash, 0)
            self.assertTrue(math.isclose(after + fee, before, rel_tol=1e-12))
            self.assertTrue(math.isclose(fee, sum(abs(value) for value in trades.values()) * rate, rel_tol=1e-12, abs_tol=1e-12))
            for asset in values:
                self.assertAlmostEqual(after_values[asset] / after, targets[asset])
            values = after_values

    def test_rejects_invalid_cost_rates_and_targets(self):
        panel = self.constant_panel()
        for rate in [-1, 10000, float("inf"), float("nan")]:
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                backtest(panel, {**self.config, "lookback_days": 1, "transaction_cost_bps": rate}, "cash")
        for target in [{"UNKNOWN": 0.5}, {"AAA": -0.1}, {"AAA": float("nan")},
                       {"AAA": float("inf")}, {"AAA": 0.6, "BBB": 0.6}]:
            with self.subTest(target=target), patch.dict(STRATEGIES, {"bad": lambda *args: target}):
                with self.assertRaises(ValueError):
                    backtest(panel, {**self.config, "lookback_days": 1}, "bad")

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

    def test_saved_experiments_are_idempotent_and_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "research.sqlite3"
            identifier, results = run_experiment(self.data, self.config, ["equal_weight", "cash"], database)
            panel = load_price_csv(self.data)
            db = setup_research_db(database)
            try:
                before = list(db.iterdump())
                save_experiment(db, identifier, panel, self.config, reversed(results))
                self.assertEqual(list(db.iterdump()), before)
                save_experiment(db, identifier, replace(panel, source="relocated.csv"), self.config, results)
                self.assertEqual(list(db.iterdump()), before)
                altered = replace(results[0], metrics={**results[0].metrics, "ending_equity": 1})
                for config, items in [(self.config, [altered, results[1]]),
                                      (self.config, results[:1]),
                                      ({**self.config, "lookback_days": 99}, results)]:
                    with self.subTest(config=config, strategies=len(items)), self.assertRaisesRegex(ValueError, "immutable"):
                        save_experiment(db, identifier, panel, config, items)
                    self.assertEqual(list(db.iterdump()), before)
                save_experiment(db, "separate-run", panel, self.config, results)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 2)
            finally:
                db.close()

    def test_failed_database_write_rolls_back_all_records(self):
        panel = load_price_csv(self.data)
        result = backtest(panel, self.config, "equal_weight")
        broken = replace(result, portfolio=result.portfolio + result.portfolio[:1])
        db = setup_research_db(":memory:")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                save_experiment(db, "broken", panel, self.config, [broken])
            for table in ["experiments", "predictions", "trades", "portfolio_daily", "metrics"]:
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            save_experiment(db, "broken", panel, self.config, [result])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], 1)
        finally:
            db.close()

    def test_rejects_duplicate_strategy_runs(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            run_experiment(self.data, self.config, ["cash", "cash"], ":memory:")

    def test_rejects_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("date,asset,close\n2025-01-01,AAA,1\n2025-01-02,BBB,1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_price_csv(path)
        panel = self.constant_panel()
        with self.assertRaisesRegex(ValueError, "consecutive daily prices"):
            backtest(replace(panel, dates=(panel.dates[0], panel.dates[1], panel.dates[3])),
                     {**self.config, "lookback_days": 1}, "cash")


if __name__ == "__main__":
    unittest.main()
