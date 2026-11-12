import copy
import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from daily_walkforward import audit_daily, propose_correction, resolve_config, simulate_daily
from historical_replay import DailyReplay, daily_close, utc_timestamp
from research_system import PricePanel, backtest_fixed_mix


class DailyWalkforwardTests(unittest.TestCase):
    def setUp(self):
        self.dates = tuple((date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(100))
        prices = {on: {"AAA": 100 * 0.99 ** i, "BBB": 150 * 0.995 ** i} for i, on in enumerate(self.dates)}
        self.panel = PricePanel(self.dates, ("AAA", "BBB"), prices, "synthetic", "synthetic")
        self.raw = {"initial_capital": 10000, "max_weight": 0.6, "transaction_cost_bps": 10,
                    "lookback_days": 2, "start_at": daily_close(self.dates[5]).isoformat(),
                    "end_at": daily_close(self.dates[-1]).isoformat(), "daily_research": {
                        "train_days": 8, "validation_days": 4, "uncertainty_penalty": 0,
                        "initial_variant": "equal_weight", "candidates": [
                            {"name": "equal_weight", "strategy": "equal_weight", "lookback_days": 2,
                             "hypothesis": "A fixed asset basket is the starting policy."},
                            {"name": "cash", "strategy": "cash", "lookback_days": 1,
                             "hypothesis": "Reduce risk if the basket loses money."}]}}

    def test_loss_reflection_causes_forward_revision_without_reset_or_early_execution(self):
        results, reviews, versions, _ = simulate_daily(self.panel, self.raw)
        self.assertTrue(versions)
        first = versions[0]
        self.assertEqual(first["to_variant"], "cash")
        frozen = next(r for r in reviews if r["as_of"] == first["frozen_at"])
        self.assertIn("net_loss", frozen["issues"])
        self.assertEqual(frozen["active_variant"], "equal_weight")
        active = next(r for r in reviews if r["active_version"] == first["version_id"])
        self.assertEqual(utc_timestamp(active["as_of"]) - utc_timestamp(first["frozen_at"]), timedelta(days=1))
        result = results[-1]
        execution = next(p for p in result.predictions if p["decision_date"] == active["date"])
        self.assertGreater(utc_timestamp(execution["execution_at"]), utc_timestamp(active["as_of"]))
        self.assertGreater(active["equity"], 0)
        self.assertLess(active["equity"], self.raw["initial_capital"])
        self.assertEqual(result.predictions[0]["decision_date"], self.dates[5])
        cash, units = self.raw["initial_capital"], dict.fromkeys(self.panel.assets, 0.0)
        for row in result.portfolio:
            for trade in (t for t in result.trades if t["date"] == row["date"]):
                signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
                cash -= signed + trade["fee"]
                units[trade["asset"]] += signed / trade["price"]
            self.assertAlmostEqual(cash, row["cash"])
            self.assertAlmostEqual(cash + math.fsum(units[a] * self.panel.price(row["date"], a) for a in units), row["equity"])
        audit, _ = audit_daily(self.panel, resolve_config(self.raw), results, reviews, versions)
        self.assertTrue(audit["passed"], audit)

    def test_future_changes_or_truncation_do_not_change_earlier_reviews_or_versions(self):
        full = simulate_daily(self.panel, self.raw)
        cutoff = self.dates[40]
        changed = replace(self.panel, content_hash="future-changed", prices={
            on: {a: v * (100 if on > cutoff else 1) for a, v in row.items()} for on, row in self.panel.prices.items()})
        truncated = replace(self.panel, dates=self.dates[:41])
        for panel in (changed, truncated):
            results, reviews, versions, _ = simulate_daily(panel, self.raw)
            self.assertEqual([r for r in full[1] if r["date"] <= cutoff], [r for r in reviews if r["date"] <= cutoff])
            self.assertEqual([v for v in full[2] if utc_timestamp(v["frozen_at"]) <= daily_close(cutoff)],
                             [v for v in versions if utc_timestamp(v["frozen_at"]) <= daily_close(cutoff)])
            for first, second in zip(full[0], results):
                for field, key in (("portfolio", "date"), ("trades", "date"), ("predictions", "decision_date")):
                    self.assertEqual([r for r in getattr(first, field) if r[key] <= cutoff],
                                     [r for r in getattr(second, field) if r[key] <= cutoff])

    def test_validation_rejects_training_winner_without_searching_for_validation_winner(self):
        policy = resolve_config(self.raw)["daily_research"]
        history = DailyReplay(self.dates, self.panel.prices).advance_to(daily_close(self.dates[20]))
        observations = [{"as_of": daily_close(self.dates[i]).isoformat(),
                         "returns": {"equal_weight": 0.01 if i < 8 else -0.02, "cash": 0.0}} for i in range(12)]
        proposal = propose_correction(history, observations, "cash", policy, {"equal_weight": 0.0, "cash": 0.0})
        self.assertEqual(proposal["proposed_variant"], "equal_weight")
        self.assertEqual(proposal["action"], "hold")
        self.assertEqual(proposal["reason"], "validation_rejected")
        with self.assertRaisesRegex(ValueError, "future"):
            propose_correction(history, observations + [{"as_of": daily_close(self.dates[21]).isoformat()}],
                               "cash", policy, {"equal_weight": 0, "cash": 0})

    def test_actual_drawdown_stop_can_reduce_risk_before_training_and_waits_for_delay(self):
        raw = copy.deepcopy(self.raw)
        raw["daily_research"].update(train_days=100, max_drawdown=0.003, activation_delay_days=3)
        _, reviews, versions, _ = simulate_daily(self.panel, raw)
        self.assertEqual(len(versions), 1)
        entry = versions[0]
        self.assertEqual(entry["reason"], "actual_drawdown_stop")
        self.assertEqual(entry["to_variant"], "cash")
        self.assertLess(len([r for r in reviews if r["as_of"] <= entry["frozen_at"]]), 100)
        pending = [r for r in reviews if entry["frozen_at"] < r["as_of"] < entry["effective_at"]]
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(r["active_variant"] == "equal_weight" for r in pending))
        self.assertTrue(all(r["reflection"]["reason"] == "revision_waiting_for_activation" for r in pending))
        self.assertEqual(next(r for r in reviews if r["as_of"] == entry["effective_at"])["active_variant"], "cash")

    def test_not_enough_days_holds_and_fixed_control_matches_existing_engine(self):
        raw = copy.deepcopy(self.raw)
        raw["daily_research"].update(train_days=100, validation_days=60)
        results, reviews, versions, _ = simulate_daily(self.panel, raw)
        self.assertEqual(versions, [])
        self.assertTrue(all(r["reflection"]["reason"] == "insufficient_completed_days" for r in reviews))
        baseline = backtest_fixed_mix(self.panel, {**raw, "strategy_budgets": {"equal_weight": 0.5, "cash": 0.5}})[-1]
        for result in (results[0], results[-1]):
            self.assertEqual(result.portfolio, baseline.portfolio)
            self.assertEqual(result.trades, baseline.trades)
            self.assertEqual(result.metrics, baseline.metrics)

    def test_audit_detects_tampering_and_config_rejects_unsafe_protocols(self):
        results, reviews, versions, _ = simulate_daily(self.panel, self.raw)
        damaged = copy.deepcopy(reviews)
        damaged[-1]["history_hash"] = "tampered"
        audit, _ = audit_daily(self.panel, resolve_config(self.raw), results, damaged, versions)
        self.assertFalse(audit["passed"])
        for updates in ({"activation_delay_days": 0}, {"strategy_budget": 2}, {"train_days": True},
                        {"candidates": []}, {"max_drawdown": float("nan")}, {"unknown": 1}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                resolve_config({**self.raw, "daily_research": {**self.raw["daily_research"], **updates}})

    def test_cli_records_every_day_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "prices.csv"
            data.write_text("date,asset,close\n" + "".join(f"{on},{a},{v}\n" for on, row in self.panel.prices.items()
                                                         for a, v in row.items()), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({**self.raw, "data": str(data)}), encoding="utf-8")
            command = [sys.executable, str(Path(__file__).with_name("daily_walkforward.py")), "--config", str(config),
                       "--output", str(root / "run")]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads((root / "run/summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["audit"]["passed"])
            self.assertEqual(summary["reviews"], 95)
            self.assertEqual(summary["completed_daily_intervals"], 94)
            self.assertEqual(len((root / "run/daily-reviews.jsonl").read_text(encoding="utf-8").splitlines()), 95)
            before = (root / "run/summary.json").read_bytes()
            repeated = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual((root / "run/summary.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
