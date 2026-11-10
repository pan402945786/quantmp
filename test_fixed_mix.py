import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from research_system import (
    PricePanel, STRATEGIES, backtest, backtest_fixed_mix, merge_strategy_targets,
    load_price_csv, run_fixed_mix_experiment, save_experiment, setup_research_db,
)


class FixedMixTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent
        self.dates = tuple(f"2025-01-{day:02d}" for day in range(1, 7))
        self.prices = {on: {"AAA": 10.0, "BBB": 20.0} for on in self.dates}
        self.panel = PricePanel(self.dates, ("AAA", "BBB"), self.prices, "test", "test")
        self.config = {"initial_capital": 100, "lookback_days": 1, "max_weight": 1,
                       "transaction_cost_bps": 100, "strategy_budgets": {"equal_weight": 1}}

    def test_full_budget_matches_single_strategy_with_identical_execution(self):
        results = backtest_fixed_mix(self.panel, self.config)
        self.assertEqual([row.role for row in results], ["shadow"] * 4 + ["portfolio"])
        actual = results[-1]
        for shadow in results[:-1]:
            independent = backtest(self.panel, self.config, shadow.strategy)
            self.assertEqual(replace(shadow, role="standalone"), independent)
        single = backtest(self.panel, self.config, "equal_weight")
        for field in ("predictions", "trades", "portfolio", "metrics", "order_events"):
            self.assertEqual(getattr(actual, field), getattr(single, field))
        self.assertEqual(actual.strategy, "fixed_mix")
        self.assertEqual(len(actual.allocations), len(actual.predictions))

    def test_shared_clock_calls_every_strategy_once_and_preserves_unfunded_shadows(self):
        calls = {name: [] for name in STRATEGIES}
        originals = dict(STRATEGIES)

        def wrapped(name):
            def target(on, history, config):
                calls[name].append((on, id(history), history.content_hash))
                self.assertNotIn("strategy_budgets", config)
                return originals[name](on, history, config)
            return target

        with patch.dict(STRATEGIES, {name: wrapped(name) for name in originals}):
            results = backtest_fixed_mix(self.panel, {**self.config, "strategy_budgets": {}})
        count = len(self.dates) - 1
        for name in calls:
            self.assertEqual(len(calls[name]), count)
            self.assertEqual(calls[name], calls["momentum"])
        actual = results[-1]
        self.assertEqual(actual.metrics["ending_equity"], 100)
        self.assertEqual(actual.trades, ())
        self.assertTrue(next(row for row in results if row.strategy == "equal_weight").trades)

    def test_unused_budget_and_strategy_cash_remain_cash(self):
        config = {**self.config, "strategy_budgets": {"equal_weight": 0.5, "cash": 0.2}}
        actual = backtest_fixed_mix(self.panel, config)[-1]
        first = actual.allocations[0]
        self.assertEqual(first["asset_targets"], {"AAA": 0.25, "BBB": 0.25})
        self.assertAlmostEqual(first["unallocated_cash_budget"], 0.3)
        self.assertEqual(first["cash_target"], 0.5)
        row = actual.portfolio[1]
        self.assertAlmostEqual(row["equity"], 100 / 1.005)
        self.assertAlmostEqual(row["cash"] / row["equity"], 0.5)

    def test_overlapping_assets_are_merged_with_the_shared_cap(self):
        components = {"s1": {"AAA": 0.6}, "s2": {"AAA": 0.6}}
        targets = merge_strategy_targets(components, {"s1": 0.5, "s2": 0.5}, self.panel.assets, 0.6)
        self.assertEqual(targets, {"AAA": 0.6})
        with patch.dict(STRATEGIES, {"s1": lambda *args: {"AAA": 0.6},
                                     "s2": lambda *args: {"AAA": 0.6}}, clear=True):
            actual = backtest_fixed_mix(self.panel, {**self.config, "max_weight": 0.6,
                                                    "strategy_budgets": {"s1": 0.5, "s2": 0.5}})[-1]
        self.assertEqual(len([t for t in actual.trades if t["date"] == self.dates[2]]), 1)
        for row in actual.portfolio:
            self.assertLessEqual(row["weights"]["AAA"], 0.6 + 1e-12)
        self.assertAlmostEqual(actual.allocations[0]["cash_target"], 0.4)

    def test_opposing_strategy_changes_cancel_before_fees(self):
        def s1(on, *args):
            return {"AAA": 1.0} if int(on[-2:]) % 2 == 0 else {}

        def s2(on, *args):
            return {} if int(on[-2:]) % 2 == 0 else {"AAA": 1.0}

        with patch.dict(STRATEGIES, {"s1": s1, "s2": s2}, clear=True):
            results = backtest_fixed_mix(self.panel, {**self.config, "strategy_budgets": {"s1": 0.5, "s2": 0.5}})
        actual = results[-1]
        self.assertTrue(all(row["asset_targets"] == {"AAA": 0.5} for row in actual.allocations))
        self.assertAlmostEqual(actual.metrics["ending_equity"], 100 / 1.005)
        self.assertEqual(len(actual.trades), 1)
        self.assertAlmostEqual(actual.metrics["total_fees"], actual.trades[0]["notional"] * 0.01)
        shadow_equity = sum(row.metrics["ending_equity"] * 0.5 for row in results[:-1])
        shadow_fees = sum(row.metrics["total_fees"] * 0.5 for row in results[:-1])
        self.assertNotAlmostEqual(actual.metrics["ending_equity"], shadow_equity)
        self.assertLess(actual.metrics["total_fees"], shadow_fees)

    def test_rotations_and_cash_exit_keep_the_same_funded_ledger(self):
        def rotate(on, *args):
            return {"2025-01-02": {"AAA": 1.0}, "2025-01-03": {"BBB": 1.0}}.get(on, {})

        with patch.dict(STRATEGIES, {"rotate": rotate, "cash": lambda *args: {}}, clear=True):
            actual = backtest_fixed_mix(self.panel, {**self.config, "strategy_budgets": {"rotate": 1}})[-1]
        bought = 100 / 1.01
        rotated = bought * 0.99 / 1.01
        liquidated = rotated * 0.99
        expected = [100, bought, rotated, liquidated, liquidated]
        for row, equity in zip(actual.portfolio, expected):
            self.assertAlmostEqual(row["equity"], equity)
        self.assertAlmostEqual(actual.metrics["total_fees"] + actual.metrics["ending_equity"], 100)
        self.assertEqual(actual.portfolio[-1]["weights"], {"AAA": 0, "BBB": 0})
        self.assertAlmostEqual(actual.portfolio[-1]["cash"], liquidated)

    def test_budgets_are_frozen_before_strategy_callbacks(self):
        config = {**self.config, "strategy_budgets": {"probe": 0.5}}

        def probe(*args):
            config["strategy_budgets"]["probe"] = 0
            return {"AAA": 1.0}

        with patch.dict(STRATEGIES, {"probe": probe}, clear=True):
            actual = backtest_fixed_mix(self.panel, config)[-1]
        self.assertTrue(all(row["strategy_budgets"] == {"probe": 0.5} for row in actual.allocations))
        self.assertTrue(all(row["asset_targets"] == {"AAA": 0.5} for row in actual.allocations))

    def test_invalid_budgets_are_rejected(self):
        cases = [None, [], "momentum", {"unknown": 0}, {"equal_weight": -0.1},
                 {"equal_weight": 1.1}, {"equal_weight": float("nan")},
                 {"equal_weight": float("inf")}, {"equal_weight": None},
                 {"equal_weight": 0.6, "cash": 0.6}]
        for budgets in cases:
            with self.subTest(budgets=budgets), self.assertRaises(ValueError):
                backtest_fixed_mix(self.panel, {**self.config, "strategy_budgets": budgets})

    def test_mix_allocations_and_all_accounts_have_invariant_history_prefix(self):
        prices = {on: {"AAA": 10 + i, "BBB": 20 - i} for i, on in enumerate(self.dates)}
        panel = replace(self.panel, prices=prices)
        config = {**self.config, "strategy_budgets": {"momentum": 0.5, "relative_reversion": 0.5}}
        baseline = backtest_fixed_mix(panel, config)
        cutoff = self.dates[3]
        changed = {on: {a: v * (100 if on > cutoff else 1) for a, v in row.items()} for on, row in prices.items()}
        variants = [replace(panel, prices=changed, content_hash="changed"),
                    replace(panel, dates=self.dates[:4], content_hash="truncated"),
                    replace(panel, dates=self.dates + ("2025-01-07",),
                            prices={**prices, "2025-01-07": {"AAA": 50.0, "BBB": 2.0}}, content_hash="appended")]
        for variant in variants:
            actual = backtest_fixed_mix(variant, config)
            for first, second in zip(baseline, actual):
                with self.subTest(variant=variant.content_hash, account=first.strategy):
                    for name, key in [("predictions", "decision_date"), ("portfolio", "date"),
                                      ("trades", "date"), ("allocations", "decision_date")]:
                        self.assertEqual([row for row in getattr(first, name) if row[key] <= cutoff],
                                         [row for row in getattr(second, name) if row[key] <= cutoff])

    def test_cash_holdings_equity_and_fees_reconcile_for_each_account(self):
        prices = {on: {"AAA": 10 + i, "BBB": 20 - i} for i, on in enumerate(self.dates)}
        panel = replace(self.panel, prices=prices)
        config = {**self.config, "strategy_budgets": {"momentum": 0.5, "relative_reversion": 0.5}}
        for result in backtest_fixed_mix(panel, config):
            cash = config["initial_capital"]
            units = dict.fromkeys(panel.assets, 0.0)
            for row in result.portfolio:
                for trade in (t for t in result.trades if t["date"] == row["date"]):
                    signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
                    cash -= signed + trade["fee"]
                    units[trade["asset"]] += signed / trade["price"]
                value = sum(units[a] * panel.price(row["date"], a) for a in panel.assets)
                self.assertAlmostEqual(cash, row["cash"])
                self.assertAlmostEqual(cash + value, row["equity"])
                self.assertGreaterEqual(row["cash"], 0)
            self.assertAlmostEqual(result.metrics["total_fees"], sum(t["fee"] for t in result.trades))

    def test_portfolio_persistence_is_atomic_idempotent_and_immutable(self):
        data = self.root / "data/demo_prices.csv"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "mix.sqlite3"
            identifier, results = run_fixed_mix_experiment(data, self.config, database)
            with closing(setup_research_db(database)) as db:
                before = list(db.iterdump())
                config = json.loads(db.execute("SELECT config_json FROM experiments").fetchone()[0])
                self.assertEqual(config["strategy_budgets"]["equal_weight"], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM account_roles WHERE role='shadow'").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM account_roles WHERE role='portfolio'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM allocation_decisions").fetchone()[0], len(results[-1].allocations))
            repeated, again = run_fixed_mix_experiment(data, self.config, database)
            self.assertEqual((identifier, results), (repeated, again))
            panel = load_price_csv(data)
            with closing(setup_research_db(database)) as db:
                self.assertEqual(list(db.iterdump()), before)
                altered = replace(results[-1], allocations=({**results[-1].allocations[0], "cash_target": 0.99},)
                                  + results[-1].allocations[1:])
                with self.assertRaisesRegex(ValueError, "immutable"):
                    save_experiment(db, identifier, panel, config, [*results[:-1], altered])
                with self.assertRaisesRegex(ValueError, "immutable"):
                    save_experiment(db, identifier, panel, config, [replace(results[0], role="portfolio"), *results[1:]])
                self.assertEqual(list(db.iterdump()), before)
            db = setup_research_db(":memory:")
            try:
                broken = replace(results[-1], allocations=results[-1].allocations + results[-1].allocations[:1])
                before = list(db.iterdump())
                with self.assertRaises(sqlite3.IntegrityError):
                    save_experiment(db, "broken", panel, config, [*results[:-1], broken])
                self.assertEqual(list(db.iterdump()), before)
            finally:
                db.close()

    def test_cli_portfolio_mode_exposes_roles_costs_and_budget_override(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cli.sqlite3"
            args = [sys.executable, str(self.root / "research.py"), "--portfolio", "--config",
                    str(self.root / "research_mix_config.json"), "--data", str(self.root / "data/demo_prices.csv"),
                    "--database", str(database), "--budget", "equal_weight=0.4", "--budget", "cash=0.2", "--json"]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)["results"]
            self.assertEqual([row["role"] for row in rows], ["shadow"] * 4 + ["portfolio"])
            self.assertTrue(all(math.isfinite(row["total_fees"]) for row in rows))
            with closing(sqlite3.connect(database)) as db:
                allocation = json.loads(db.execute("SELECT allocation_json FROM allocation_decisions LIMIT 1").fetchone()[0])
                self.assertAlmostEqual(allocation["cash_target"], 0.6)
                self.assertAlmostEqual(allocation["unallocated_cash_budget"], 0.4)
            invalid_args = args.copy()
            invalid_args.remove("--portfolio")
            invalid = subprocess.run(invalid_args, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("requires --portfolio", invalid.stderr)
            invalid = subprocess.run(args + ["--budget", "cash=0.1"], capture_output=True, text=True)
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("duplicate strategy budget", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
