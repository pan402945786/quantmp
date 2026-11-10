import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from pathlib import Path

from historical_replay import DailyReplay, UnavailableDataError, daily_close, utc_timestamp
from research_system import (
    PricePanel, STRATEGIES, backtest_dynamic_mix, backtest_fixed_mix, load_price_csv,
    run_dynamic_mix_experiment, save_experiment, setup_research_db,
)
from strategy_selector import (
    FeedbackBook, FeedbackView, MatureFeedback, RegimeSelector, SelectorConfig, fit_selector,
)


class StrategySelectorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent
        self.days = tuple((date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(75))
        self.prices = {on: {"AAA": 100.0} for on in self.days}
        self.small = {"candidates": ["momentum", "relative_reversion"], "horizon_days": 1,
                      "train_days": 12, "validation_days": 8, "research_every_days": 3,
                      "activation_delay_days": 1, "min_train_samples": 2, "min_state_samples": 2,
                      "min_validation_samples": 2, "min_active_validation_samples": 1,
                      "uncertainty_penalty": 0, "strategy_budget": 1}
        self.config = {"initial_capital": 1000, "lookback_days": 2, "max_weight": 0.6,
                       "transaction_cost_bps": 10, "strategy_budgets": {"momentum": 0.5, "relative_reversion": 0.5},
                       "selector": self.small}

    def history(self, on):
        return DailyReplay(self.days, self.prices).advance_to(daily_close(on))

    def feedback(self, clock, negative_validation=False):
        cutoff = clock - timedelta(days=self.small["validation_days"])
        rows = []
        for on in self.days:
            execution = (date.fromisoformat(on) + timedelta(days=1)).isoformat()
            end = (date.fromisoformat(on) + timedelta(days=2)).isoformat()
            observed = daily_close(end)
            if observed > clock:
                continue
            for name in self.small["candidates"]:
                value = 0.03 if name == "momentum" else 0.01
                if negative_validation and daily_close(on) >= cutoff and name == "momentum":
                    value = -0.04
                rows.append(MatureFeedback(f"{name}-{on}", name, on, daily_close(on), execution, end,
                                           observed, observed, "up", value))
        return FeedbackView(clock, tuple(rows))

    def panel(self):
        prices = {}
        for i, on in enumerate(self.days[:60]):
            a = 100 * 1.01 ** min(i, 34) * 0.98 ** max(0, i - 34)
            b = 100 * 1.003 ** min(i, 34) * 0.99 ** max(0, i - 34)
            prices[on] = {"AAA": a, "BBB": b}
        return PricePanel(self.days[:60], ("AAA", "BBB"), prices, "test", "test")

    def test_feedback_matures_after_execution_horizon_and_includes_entry_fees(self):
        replay = DailyReplay(self.days, self.prices, 10)
        book = FeedbackBook(1, 10, "test-v1")
        initial = replay.advance_to(daily_close(self.days[0]) + timedelta(seconds=10))
        book.advance(initial, {"s": 100}, {"s": 100})
        created = book.register(initial, ["s"], "up")[0]
        self.assertEqual(book.view(initial.as_of).samples, ())
        entry = replay.advance_to(daily_close(self.days[1]) + timedelta(seconds=10))
        self.assertEqual(book.advance(entry, {"s": 100}, {"s": 99}), [])
        self.assertEqual(book.view(entry.as_of).samples, ())
        final = replay.advance_to(daily_close(self.days[2]) + timedelta(seconds=10))
        matured = book.advance(final, {"s": 105}, {"s": 104})
        sample = book.view(final.as_of).samples[0]
        self.assertAlmostEqual(sample.net_return, 0.04)
        self.assertEqual(sample.mature_at, final.as_of)
        self.assertEqual(matured[0]["payload"]["sample_id"], created["payload"]["sample_id"])
        with self.assertRaisesRegex(ValueError, "immature"):
            FeedbackView(entry.as_of, (sample,))
        with self.assertRaises(UnavailableDataError):
            book.view(final.as_of + timedelta(days=1))
        with self.assertRaises(FrozenInstanceError):
            sample.net_return = 99

    def test_feedback_rejects_duplicate_decisions_and_missing_execution(self):
        book = FeedbackBook(1, 0, "test")
        history = self.history(self.days[0])
        book.advance(history, {"s": 100}, {"s": 100})
        book.register(history, ["s"], "up")
        with self.assertRaisesRegex(ValueError, "already registered"):
            book.register(history, ["s"], "down_or_flat")
        with self.assertRaisesRegex(ValueError, "missing feedback execution"):
            book.advance(self.history(self.days[2]), {"s": 100}, {"s": 100})

    def test_training_purges_cross_boundary_labels_and_uses_nonoverlapping_periods(self):
        history = self.history(self.days[55])
        config = SelectorConfig.parse(self.small, STRATEGIES)
        feedback = self.feedback(history.as_of)
        version, evidence = fit_selector(history, feedback, config)
        self.assertIsNotNone(version)
        self.assertEqual(version.status, "validated")
        rows = {sample.sample_id: sample for sample in feedback.samples}
        split = utc_timestamp(evidence["validation_start"])
        self.assertTrue(evidence["training_ids"])
        self.assertTrue(evidence["validation_ids"])
        self.assertFalse(set(evidence["training_ids"]) & set(evidence["validation_ids"]))
        self.assertTrue(all(rows[key].observed_at <= split for key in evidence["training_ids"]))
        self.assertTrue(all(rows[key].decision_at >= split for key in evidence["validation_ids"]))
        crossed = [sample for sample in feedback.samples if sample.decision_at < split < sample.observed_at]
        self.assertTrue(crossed)
        self.assertTrue(all(sample.sample_id not in evidence["training_ids"] for sample in crossed))
        for name in self.small["candidates"]:
            used = sorted([rows[key] for key in evidence["training_ids"] + evidence["validation_ids"]
                           if rows[key].strategy == name], key=lambda s: s.execution_date)
            self.assertTrue(all(previous.end_date < current.execution_date for previous, current in zip(used, used[1:])))

    def test_validation_does_not_refit_to_the_validation_winner(self):
        history = self.history(self.days[55])
        config = SelectorConfig.parse(self.small, STRATEGIES)
        good, first = fit_selector(history, self.feedback(history.as_of), config)
        bad, second = fit_selector(history, self.feedback(history.as_of, True), config)
        self.assertEqual(first["estimates"], second["estimates"])
        self.assertEqual(first["proposed_policy"], second["proposed_policy"])
        self.assertEqual(good.policy["up"], "momentum")
        self.assertEqual(bad.status, "fallback_cash")
        self.assertEqual(set(bad.policy.values()), {"cash"})
        self.assertEqual(second["reason"], "validation_failed_cash_fallback")

    def test_new_versions_are_invisible_and_unusable_until_the_effective_time(self):
        history = self.history(self.days[55])
        config = SelectorConfig.parse(self.small, STRATEGIES)
        selector = RegimeSelector(config)
        events = selector.advance(history, self.feedback(history.as_of))
        frozen = next(event["payload"] for event in events if event["event_type"] == "version_frozen")
        self.assertEqual(utc_timestamp(frozen["effective_at"]), history.as_of + timedelta(days=1))
        self.assertEqual(selector.visible_versions(history.as_of), ())
        self.assertEqual(selector.decide(history, "up")[0], {})
        with self.assertRaises(UnavailableDataError):
            selector.visible_versions(history.as_of + timedelta(days=1))
        later = self.history(self.days[56])
        selector.advance(later, self.feedback(later.as_of))
        budgets, decision = selector.decide(later, "up")
        self.assertEqual(budgets, {"momentum": 1})
        self.assertEqual(decision["selector_version"], frozen["version_id"])
        self.assertEqual(len(selector.visible_versions(later.as_of)), 1)
        with self.assertRaises(TypeError):
            selector.visible_versions(later.as_of)[0].policy["up"] = "cash"
        with self.assertRaisesRegex(ValueError, "current clock"):
            selector.decide(history, "up")

    def test_insufficient_feedback_keeps_cash_without_manufacturing_labels(self):
        history = self.history(self.days[0])
        selector = RegimeSelector(SelectorConfig.parse(self.small, STRATEGIES))
        events = selector.advance(history, FeedbackView(history.as_of, ()))
        self.assertEqual([event["event_type"] for event in events], ["research_rejected"])
        self.assertEqual(selector.decide(history, "up")[0], {})
        self.assertEqual(selector.visible_versions(history.as_of), ())

    def test_dynamic_switching_uses_a_continuous_ledger_and_preserves_controls(self):
        panel = self.panel()
        results = backtest_dynamic_mix(panel, self.config)
        self.assertEqual([row.role for row in results], ["shadow"] * 4 + ["benchmark", "portfolio"])
        fixed = backtest_fixed_mix(panel, self.config)
        self.assertEqual(results[:4], fixed[:4])
        self.assertEqual(replace(results[-2], role="portfolio"), fixed[-1])
        dynamic = results[-1]
        activations = [event for event in dynamic.selector_events if event["event_type"] == "version_activated"]
        self.assertGreater(len(activations), 1)
        self.assertTrue(dynamic.trades)
        self.assertTrue(any(row["reason"] == "validation_failed_cash_fallback" for row in dynamic.allocations))
        cash = self.config["initial_capital"]
        units = dict.fromkeys(panel.assets, 0.0)
        for row in dynamic.portfolio:
            for trade in (t for t in dynamic.trades if t["date"] == row["date"]):
                signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
                cash -= signed + trade["fee"]
                units[trade["asset"]] += signed / trade["price"]
            self.assertAlmostEqual(cash, row["cash"])
            self.assertAlmostEqual(cash + sum(units[a] * panel.price(row["date"], a) for a in panel.assets), row["equity"])
            self.assertGreaterEqual(row["cash"], 0)
        first_effective = utc_timestamp(activations[0]["event_at"])
        for row in dynamic.allocations:
            clock = utc_timestamp(row["decision_at"])
            if clock < first_effective:
                self.assertEqual(row["asset_targets"], {})
            if row["selector_version"] is not None:
                self.assertLessEqual(utc_timestamp(row["selector_effective_at"]), clock)
        frozen_ids = {event["payload"]["version_id"] for event in dynamic.selector_events if event["event_type"] == "version_frozen"}
        self.assertTrue(all(event["payload"]["version_id"] in frozen_ids for event in activations))

    def test_future_changes_cannot_change_research_versions_labels_or_decisions(self):
        panel = self.panel()
        cutoff = panel.dates[35]
        baseline = backtest_dynamic_mix(panel, self.config)
        changed = {on: {a: v * (1.7 if on > cutoff else 1) for a, v in row.items()} for on, row in panel.prices.items()}
        appended_date = (date.fromisoformat(panel.dates[-1]) + timedelta(days=1)).isoformat()
        variants = [replace(panel, prices=changed, content_hash="changed"),
                    replace(panel, dates=panel.dates[:36], content_hash="truncated"),
                    replace(panel, dates=panel.dates + (appended_date,),
                            prices={**panel.prices, appended_date: {"AAA": 200, "BBB": 200}}, content_hash="appended")]
        for variant in variants:
            result = backtest_dynamic_mix(variant, self.config)
            for first, second in zip(baseline, result):
                with self.subTest(variant=variant.content_hash, account=first.strategy):
                    for name, key in (("predictions", "decision_date"), ("trades", "date"),
                                      ("portfolio", "date"), ("allocations", "decision_date")):
                        self.assertEqual([row for row in getattr(first, name) if row[key] <= cutoff],
                                         [row for row in getattr(second, name) if row[key] <= cutoff])
                    boundary = daily_close(cutoff)
                    self.assertEqual([e for e in first.selector_events if utc_timestamp(e["event_at"]) <= boundary],
                                     [e for e in second.selector_events if utc_timestamp(e["event_at"]) <= boundary])

    def test_end_of_run_preserves_pending_feedback_and_does_not_complete_it(self):
        panel = self.panel()
        dynamic = backtest_dynamic_mix(panel, {**self.config, "data_delay_seconds": 60})[-1]
        created = {e["payload"]["sample_id"]: e for e in dynamic.selector_events if e["event_type"] == "feedback_created"}
        matured = {e["payload"]["sample_id"]: e for e in dynamic.selector_events if e["event_type"] == "feedback_matured"}
        self.assertEqual(len(created) - len(matured), 4 * (self.small["horizon_days"] + 1))
        last_clock = utc_timestamp(dynamic.portfolio[-1]["as_of"])
        self.assertTrue(all(utc_timestamp(e["event_at"]) <= last_clock for e in matured.values()))
        self.assertTrue(all(utc_timestamp(created[key]["payload"]["mature_at"]) > last_clock for key in set(created) - set(matured)))

    def test_matured_labels_reconcile_to_pre_execution_equity_and_all_fees(self):
        results = backtest_dynamic_mix(self.panel(), self.config)
        shadows = {result.strategy: result for result in results[:-2]}
        created = {e["payload"]["sample_id"]: e["payload"] for e in results[-1].selector_events
                   if e["event_type"] == "feedback_created"}
        for event in results[-1].selector_events:
            if event["event_type"] != "feedback_matured":
                continue
            label = created[event["payload"]["sample_id"]]
            shadow = shadows[label["strategy"]]
            equities = {row["date"]: row["equity"] for row in shadow.portfolio}
            entry_fees = math.fsum(trade["fee"] for trade in shadow.trades
                                   if trade["date"] == label["execution_date"])
            before_entry = equities[label["execution_date"]] + entry_fees
            expected = equities[label["end_date"]] / before_entry - 1
            self.assertAlmostEqual(event["payload"]["net_return"], expected)

    def test_bounded_run_does_not_train_on_prestart_or_postend_feedback(self):
        panel = self.panel()
        config = {**self.config, "start_at": daily_close(panel.dates[20]).isoformat(),
                  "end_at": daily_close(panel.dates[45]).isoformat()}
        result = backtest_dynamic_mix(panel, config)
        self.assertEqual(result[-1].portfolio[0]["equity"], self.config["initial_capital"])
        self.assertTrue(all(row["decision_date"] >= panel.dates[20] for row in result[-1].predictions))
        self.assertEqual(result, backtest_dynamic_mix(replace(panel, dates=panel.dates[:46]), config))
        last_clock = utc_timestamp(config["end_at"])
        self.assertTrue(all(utc_timestamp(event["event_at"]) <= last_clock for event in result[-1].selector_events))
        feedback_ids = {event["payload"]["sample_id"] for event in result[-1].selector_events
                        if event["event_type"] == "feedback_created"}
        for event in result[-1].selector_events:
            if event["event_type"] == "version_frozen":
                evidence = event["payload"]["evidence"]
                self.assertTrue(set(evidence["training_ids"] + evidence["validation_ids"]) <= feedback_ids)

    def test_selector_configuration_rejects_invalid_protocols(self):
        cases = [{"candidates": ["unknown"]}, {"candidates": ["cash"]}, {"candidates": [[]]},
                 {"candidates": ["momentum", "momentum"]},
                 {"horizon_days": 0}, {"activation_delay_days": 0}, {"research_every_days": 1.5},
                 {"train_days": 1}, {"validation_days": 1}, {"uncertainty_penalty": float("nan")},
                 {"strategy_budget": 1.1}, {"min_state_samples": 1}, {"unknown": 10}]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                SelectorConfig.parse({**self.small, **extra}, STRATEGIES)

    def test_selector_events_are_atomic_immutable_and_idempotent(self):
        data = self.root / "data/demo_prices.csv"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "selector.sqlite3"
            identifier, results = run_dynamic_mix_experiment(data, self.config, database)
            with closing(setup_research_db(database)) as db:
                before = list(db.iterdump())
                config = json.loads(db.execute("SELECT config_json FROM experiments").fetchone()[0])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM selector_events").fetchone()[0], len(results[-1].selector_events))
            repeated, again = run_dynamic_mix_experiment(data, self.config, database)
            self.assertEqual((identifier, results), (repeated, again))
            panel = load_price_csv(data)
            altered_event = {**results[-1].selector_events[0], "payload": {"changed": True}}
            altered = replace(results[-1], selector_events=(altered_event,) + results[-1].selector_events[1:])
            with closing(setup_research_db(database)) as db:
                self.assertEqual(list(db.iterdump()), before)
                with self.assertRaisesRegex(ValueError, "immutable"):
                    save_experiment(db, identifier, panel, config, [*results[:-1], altered])
                self.assertEqual(list(db.iterdump()), before)
            db = setup_research_db(":memory:")
            try:
                broken = replace(results[-1], selector_events=({**altered_event, "event_at": None},))
                before = list(db.iterdump())
                with self.assertRaises(sqlite3.IntegrityError):
                    save_experiment(db, "broken", panel, config, [*results[:-1], broken])
                self.assertEqual(list(db.iterdump()), before)
            finally:
                db.close()

    def test_cli_runs_dynamic_fixed_and_shadow_accounts_with_pending_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({**self.config, "data": str(self.root / "data/demo_prices.csv")}), encoding="utf-8")
            command = [sys.executable, str(self.root / "research.py"), "--selector", "--config", str(config),
                       "--database", str(Path(directory) / "run.sqlite3"), "--json"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual([r["role"] for r in payload["results"]], ["shadow"] * 4 + ["benchmark", "portfolio"])
            self.assertEqual(payload["selector"]["pending_feedback"], 8)
            self.assertEqual(payload["results"][-1]["strategy"], "dynamic_mix")


if __name__ == "__main__":
    unittest.main()
