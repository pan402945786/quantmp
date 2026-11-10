import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from historical_replay import DailyReplay, UnavailableDataError, daily_close, utc_timestamp
from research_system import (
    STRATEGIES, PricePanel, backtest_dynamic_mix, backtest_research_mix,
    load_price_csv, run_research_mix_experiment, save_experiment, setup_research_db,
)
from strategy_research import CandidateResearch, ResearchConfig
from strategy_selector import FeedbackView, MatureFeedback


class StrategyResearchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent
        self.dates = tuple((date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(80))
        self.prices = {on: {"AAA": 100 * 1.01 ** i, "BBB": 100 * 1.005 ** i}
                       for i, on in enumerate(self.dates)}
        self.panel = PricePanel(self.dates, ("AAA", "BBB"), self.prices, "test", "test")
        self.spec = {"name": "slow_trend", "parent": "momentum", "lookback_days": 3,
                     "hypothesis": "Longer windows may reduce turnover."}
        self.research = {"candidate_plan": [self.spec], "max_attempts": 1, "attempts_per_cycle": 1,
                         "research_every_days": 2, "trigger_cash_days": 2, "min_mature_samples": 2,
                         "train_days": 6, "validation_days": 4, "shadow_delay_days": 1,
                         "confirmation_days": 6, "confirmation_timeout_days": 10,
                         "min_confirmation_samples": 2, "activation_delay_days": 1,
                         "min_return": 0, "min_parent_excess": 0, "max_drawdown": 0.35,
                         "uncertainty_penalty": 0}
        self.config = {"initial_capital": 1000, "lookback_days": 2, "max_weight": 0.6,
                       "transaction_cost_bps": 10, "strategy_version": "test-v1",
                       "strategy_budgets": {"momentum": 0.5, "relative_reversion": 0.5},
                       "selector": {"candidates": ["momentum", "relative_reversion"], "horizon_days": 1,
                                    "train_days": 12, "validation_days": 8, "research_every_days": 3,
                                    "activation_delay_days": 1, "min_train_samples": 2, "min_state_samples": 2,
                                    "min_validation_samples": 2, "min_active_validation_samples": 1,
                                    "uncertainty_penalty": 0, "strategy_budget": 1},
                       "research": self.research}

    def history(self, index):
        return DailyReplay(self.dates, self.prices).advance_to(daily_close(self.dates[index]))

    def feedback(self, history, candidate=None, start=0, candidate_return=0.04):
        samples = []
        for i, on in enumerate(self.dates):
            if i + 2 >= len(self.dates) or daily_close(self.dates[i + 2]) > history.as_of:
                continue
            names = [("momentum", 0.01)]
            if candidate is not None and i >= start:
                names.append((candidate, candidate_return))
            for name, value in names:
                mature = daily_close(self.dates[i + 2])
                samples.append(MatureFeedback(f"{name}-{on}", name, on, daily_close(on), self.dates[i + 1],
                                              self.dates[i + 2], mature, mature, "up", value))
        return FeedbackView(history.as_of, tuple(samples))

    def evaluator(self, history, spec):
        return {"data_cutoff": history.as_of.isoformat(), "history_hash": history.content_hash,
                "train": {"candidate": {"total_return": 0.10, "max_drawdown": -0.1},
                          "parent": {"total_return": 0.05}},
                "validation": {"candidate": {"total_return": 0.05, "max_drawdown": -0.1},
                               "parent": {"total_return": 0.03}}}

    def manager(self, extra=None):
        return CandidateResearch(ResearchConfig.parse({**self.research, "trigger_cash_days": 1, **(extra or {})}, STRATEGIES), 1, "test-v1")

    def frozen_manager(self, extra=None):
        manager = self.manager(extra)
        history = self.history(30)
        manager.begin_tick(history)
        events = manager.finish_tick(history, self.feedback(history), "cash", {}, self.evaluator)
        record = next(event["payload"] for event in events if event["event_type"] == "candidate_frozen")
        return manager, record

    def test_attempt_budget_is_global_and_failures_are_retained(self):
        plan = [self.spec, {**self.spec, "name": "other"}]
        manager = self.manager({"candidate_plan": plan, "max_attempts": 2, "attempts_per_cycle": 2})
        history = self.history(30)
        manager.begin_tick(history)
        calls = []

        def fail(view, spec):
            calls.append(spec.name)
            if spec.name == "other":
                raise ValueError("deterministic evaluator failure")
            evidence = self.evaluator(view, spec)
            evidence["validation"]["candidate"]["total_return"] = -0.2
            return evidence

        events = manager.finish_tick(history, self.feedback(history), "cash", {}, fail)
        self.assertEqual(calls, ["slow_trend", "other"])
        self.assertEqual(sum(e["event_type"] == "hypothesis_registered" for e in events), 2)
        self.assertEqual(sum(e["event_type"] == "research_attempt_failed" for e in events), 1)
        completed = next(e["payload"] for e in events if e["event_type"] == "research_attempt_completed")
        self.assertFalse(completed["passed"])
        self.assertEqual(completed["reason"], "internal_validation_failed")
        later = self.history(32)
        manager.begin_tick(later)
        more = manager.finish_tick(later, self.feedback(later), "cash", {}, fail)
        self.assertEqual([e["event_type"] for e in more], ["research_budget_exhausted"])
        self.assertEqual(len(calls), 2)
        registration = next(e["payload"] for e in events if e["event_type"] == "hypothesis_registered")
        self.assertTrue(registration["spec"]["hypothesis"])
        self.assertEqual(registration["parent_version"], "test-v1")
        self.assertEqual(registration["attempt"], 1)
        self.assertTrue(registration["mature_evidence_ids"])

    def test_missing_visible_history_does_not_spend_an_attempt(self):
        manager = self.manager()
        history = self.history(8)
        manager.begin_tick(history)
        events = manager.finish_tick(history, self.feedback(history), "cash", {}, self.evaluator)
        self.assertEqual([e["event_type"] for e in events], ["research_waiting"])
        self.assertEqual(events[0]["payload"]["reason"], "insufficient_visible_history")
        later = self.history(30)
        manager.begin_tick(later)
        events = manager.finish_tick(later, self.feedback(later), "cash", {}, self.evaluator)
        self.assertEqual(next(e["payload"]["attempt"] for e in events if e["event_type"] == "hypothesis_registered"), 1)

    def test_cash_trigger_requires_a_streak_and_mature_evidence(self):
        manager = self.manager({"trigger_cash_days": 2})
        first = self.history(30)
        manager.begin_tick(first)
        events = manager.finish_tick(first, self.feedback(first), "cash", {}, self.evaluator)
        self.assertFalse(any(e["event_type"] == "hypothesis_registered" for e in events))
        later = self.history(32)
        manager.begin_tick(later)
        events = manager.finish_tick(later, self.feedback(later), "momentum", {}, self.evaluator)
        self.assertFalse(any(e["event_type"] == "hypothesis_registered" for e in events))
        latest = self.history(34)
        manager.begin_tick(latest)
        events = manager.finish_tick(latest, FeedbackView(latest.as_of, ()), "cash", {}, self.evaluator)
        self.assertFalse(any(e["event_type"] == "hypothesis_registered" for e in events))

    def test_freeze_shadow_confirmation_activation_and_exit_are_distinct_events(self):
        manager, record = self.frozen_manager()
        account = record["account"]
        frozen_at = utc_timestamp(record["frozen_at"])
        self.assertEqual(manager.replacements(frozen_at), {})
        with self.assertRaises(UnavailableDataError):
            manager.replacements(frozen_at + timedelta(days=1))
        start = self.history(31)
        versions, events = manager.begin_tick(start)
        self.assertEqual([v.account for v in versions], [account])
        self.assertEqual(events[0]["event_type"], "candidate_shadow_started")
        manager.finish_tick(start, self.feedback(start), "cash", {account: 100}, self.evaluator)
        confirming = self.history(38)
        manager.begin_tick(confirming)
        events = manager.finish_tick(confirming, self.feedback(confirming, account, 31), "cash", {account: 105}, self.evaluator)
        confirmation = next(e["payload"] for e in events if e["event_type"] == "candidate_confirmed")
        self.assertEqual(manager.replacements(confirming.as_of), {})
        self.assertTrue(all(key.startswith(account) for key in confirmation["evidence"]["candidate_ids"]))
        self.assertEqual(utc_timestamp(confirmation["effective_at"]), confirming.as_of + timedelta(days=1))
        active = self.history(39)
        _, events = manager.begin_tick(active)
        self.assertEqual(events[0]["event_type"], "candidate_activated")
        self.assertEqual(manager.replacements(active.as_of)["momentum"].account, account)
        manager.finish_tick(active, self.feedback(active, account, 31), "cash", {account: 105}, self.evaluator)
        falling = self.history(41)
        manager.begin_tick(falling)
        events = manager.finish_tick(falling, self.feedback(falling, account, 31), "cash", {account: 50}, self.evaluator)
        self.assertTrue(any(e["event_type"] == "candidate_exit_scheduled" for e in events))
        self.assertIn("momentum", manager.replacements(falling.as_of))
        retired = self.history(42)
        _, events = manager.begin_tick(retired)
        self.assertEqual(events[0]["event_type"], "candidate_retired")
        self.assertEqual(manager.replacements(retired.as_of), {})

    def test_failed_or_timed_out_shadow_never_becomes_effective(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                manager, record = self.frozen_manager({"min_confirmation_samples": 100} if timeout else None)
                account = record["account"]
                start = self.history(31)
                manager.begin_tick(start)
                manager.finish_tick(start, self.feedback(start), "cash", {account: 100}, self.evaluator)
                later = self.history(42)
                manager.begin_tick(later)
                events = manager.finish_tick(later, self.feedback(later, account, 31, -0.05), "cash", {account: 100}, self.evaluator)
                rejection = next(e["payload"] for e in events if e["event_type"] == "candidate_rejected")
                self.assertEqual(rejection["reason"], "shadow_confirmation_timeout" if timeout else "shadow_confirmation_failed")
                self.assertEqual(manager.replacements(later.as_of), {})

    def test_candidate_shadow_starts_after_freeze_without_backfilled_results(self):
        results = backtest_research_mix(self.panel, self.config)
        adaptive = next(r for r in results if r.strategy == "adaptive_mix")
        candidates = [r for r in results if r.role == "candidate_shadow"]
        self.assertTrue(candidates)
        frozen = {e["payload"]["account"]: e for e in adaptive.research_events if e["event_type"] == "candidate_frozen"}
        starts = {e["payload"]["account"]: e for e in adaptive.research_events if e["event_type"] == "candidate_shadow_started"}
        for candidate in candidates:
            self.assertGreater(utc_timestamp(candidate.portfolio[0]["as_of"]), utc_timestamp(frozen[candidate.strategy]["event_at"]))
            self.assertEqual(candidate.portfolio[0]["as_of"], starts[candidate.strategy]["event_at"])
            self.assertEqual(candidate.portfolio[0]["equity"], self.config["initial_capital"])
            self.assertEqual(candidate.portfolio[0]["cash"], self.config["initial_capital"])
            self.assertGreater(candidate.portfolio[0]["date"], results[0].portfolio[0]["date"])
            labels = [e["payload"] for e in adaptive.research_events if e["event_type"] == "feedback_created"
                      and e["payload"]["strategy"] == candidate.strategy]
            self.assertTrue(labels)
            self.assertEqual(labels[0]["decision_at"], candidate.portfolio[0]["as_of"])
            self.assertEqual(labels[0]["strategy_version"], frozen[candidate.strategy]["payload"]["version_id"])

    def test_research_preserves_original_selector_and_fixed_control_exactly(self):
        original_library = dict(STRATEGIES)
        baseline = backtest_dynamic_mix(self.panel, self.config)
        result = backtest_research_mix(self.panel, self.config)
        self.assertEqual(result[:5], baseline[:5])
        self.assertEqual(replace(result[5], role="portfolio"), baseline[5])
        self.assertEqual(STRATEGIES, original_library)
        adaptive = next(r for r in result if r.strategy == "adaptive_mix")
        self.assertTrue(any(a["selected_strategy"].startswith("candidate_") for a in adaptive.allocations))

    def test_zero_attempt_budget_leaves_the_adaptive_account_equal_to_baseline(self):
        config = {**self.config, "research": {**self.research, "max_attempts": 0}}
        result = backtest_research_mix(self.panel, config)
        self.assertEqual(len(result), 7)
        baseline, adaptive = result[5:7]
        for field in ("predictions", "trades", "portfolio", "metrics", "order_events"):
            self.assertEqual(getattr(baseline, field), getattr(adaptive, field))
        self.assertEqual([e["event_type"] for e in adaptive.research_events], ["research_budget_exhausted"])

    def test_internal_research_splits_only_use_visible_prices_and_remain_research_evidence(self):
        adaptive = next(r for r in backtest_research_mix(self.panel, self.config) if r.strategy == "adaptive_mix")
        for event in adaptive.research_events:
            if event["event_type"] != "research_attempt_completed":
                continue
            evidence = event["payload"]["evidence"]
            self.assertTrue(evidence["research_only"])
            self.assertEqual(evidence["data_cutoff"], event["event_at"])
            self.assertLess(utc_timestamp(evidence["train"]["end_at"]), utc_timestamp(evidence["validation"]["start_at"]))
            self.assertLessEqual(utc_timestamp(evidence["validation"]["end_at"]), utc_timestamp(event["event_at"]))
            self.assertEqual(evidence["validation"]["last_price_date"],
                             (utc_timestamp(event["event_at"]) - timedelta(days=1)).date().isoformat())

    def test_replacement_and_exit_do_not_reset_cash_holdings_or_equity(self):
        prices = {on: {asset: value * (0.2 if i >= 48 else 1) for asset, value in self.prices[on].items()}
                  for i, on in enumerate(self.dates)}
        panel = replace(self.panel, prices=prices)
        results = backtest_research_mix(panel, self.config)
        adaptive = next(r for r in results if r.strategy == "adaptive_mix")
        self.assertTrue(any(e["event_type"] == "candidate_activated" for e in adaptive.research_events))
        self.assertTrue(any(e["event_type"] == "candidate_retired" for e in adaptive.research_events))
        cash = self.config["initial_capital"]
        units = dict.fromkeys(panel.assets, 0.0)
        for row in adaptive.portfolio:
            for trade in (t for t in adaptive.trades if t["date"] == row["date"]):
                signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
                cash -= signed + trade["fee"]
                units[trade["asset"]] += signed / trade["price"]
            self.assertAlmostEqual(cash, row["cash"])
            self.assertAlmostEqual(cash + sum(units[a] * panel.price(row["date"], a) for a in panel.assets), row["equity"])
            self.assertGreaterEqual(row["cash"], 0)
        activations = {e["payload"]["version_id"]: utc_timestamp(e["event_at"]) for e in adaptive.research_events
                       if e["event_type"] == "candidate_activated"}
        for row in adaptive.allocations:
            clock = utc_timestamp(row["decision_at"])
            for info in row["candidate_versions"].values():
                self.assertLessEqual(utc_timestamp(info["effective_at"]), clock)
                self.assertLessEqual(activations[info["version_id"]], clock)

    def test_changing_or_removing_future_does_not_change_prior_attempts_or_accounts(self):
        baseline = backtest_research_mix(self.panel, self.config)
        cutoff = self.dates[35]
        changed = {on: {a: value * (0.5 if on > cutoff else 1) for a, value in row.items()}
                   for on, row in self.prices.items()}
        next_date = (date.fromisoformat(self.dates[-1]) + timedelta(days=1)).isoformat()
        variants = [replace(self.panel, prices=changed, content_hash="changed"),
                    replace(self.panel, dates=self.dates[:36], content_hash="truncated"),
                    replace(self.panel, dates=self.dates + (next_date,),
                            prices={**self.prices, next_date: {"AAA": 200, "BBB": 200}}, content_hash="appended")]
        for variant in variants:
            result = {r.strategy: r for r in backtest_research_mix(variant, self.config)}
            for first in baseline:
                with self.subTest(variant=variant.content_hash, account=first.strategy):
                    second = result[first.strategy]
                    for field, key in (("predictions", "decision_date"), ("trades", "date"),
                                       ("portfolio", "date"), ("allocations", "decision_date")):
                        self.assertEqual([r for r in getattr(first, field) if r[key] <= cutoff],
                                         [r for r in getattr(second, field) if r[key] <= cutoff])
                    for field in ("selector_events", "research_events"):
                        boundary = daily_close(cutoff)
                        self.assertEqual([e for e in getattr(first, field) if utc_timestamp(e["event_at"]) <= boundary],
                                         [e for e in getattr(second, field) if utc_timestamp(e["event_at"]) <= boundary])

    def test_termination_keeps_unconfirmed_candidate_out_of_actual_targets(self):
        baseline = backtest_research_mix(self.panel, self.config)
        adaptive = next(r for r in baseline if r.strategy == "adaptive_mix")
        frozen = next(e for e in adaptive.research_events if e["event_type"] == "candidate_frozen")
        config = {**self.config, "end_at": frozen["event_at"]}
        result = backtest_research_mix(self.panel, config)
        actual = next(r for r in result if r.strategy == "adaptive_mix")
        self.assertFalse(any(r.role == "candidate_shadow" for r in result))
        self.assertTrue(any(e["event_type"] == "candidate_frozen" for e in actual.research_events))
        self.assertTrue(all(row["candidate_versions"] == {} for row in actual.allocations))

    def test_future_candidate_accounts_do_not_appear_in_earlier_targets_or_feedback(self):
        cutoff = self.dates[9]
        full = backtest_research_mix(self.panel, self.config)
        prefix = backtest_research_mix(replace(self.panel, dates=self.dates[:10]), self.config)
        self.assertTrue(any(r.role == "candidate_shadow" for r in full))
        self.assertFalse(any(r.role == "candidate_shadow" for r in prefix))
        actual = next(r for r in full if r.strategy == "adaptive_mix")
        short = next(r for r in prefix if r.strategy == "adaptive_mix")
        self.assertEqual([row for row in actual.allocations if row["decision_date"] <= cutoff], list(short.allocations))
        self.assertTrue(all(not any(name.startswith("candidate_") for name in row["strategy_targets"])
                            for row in short.allocations))
        self.assertFalse(any(e["event_type"] == "candidate_frozen" for e in short.research_events))

    def test_candidate_labels_mature_from_the_forward_shadow_ledger_only(self):
        results = backtest_research_mix(self.panel, self.config)
        adaptive = next(r for r in results if r.strategy == "adaptive_mix")
        candidates = {r.strategy: r for r in results if r.role == "candidate_shadow"}
        created = {e["payload"]["sample_id"]: e["payload"] for e in adaptive.research_events
                   if e["event_type"] == "feedback_created"}
        matured = 0
        for event in adaptive.research_events:
            if event["event_type"] != "feedback_matured":
                continue
            label = created[event["payload"]["sample_id"]]
            shadow = candidates[label["strategy"]]
            equities = {row["date"]: row["equity"] for row in shadow.portfolio}
            entry_fees = math.fsum(trade["fee"] for trade in shadow.trades if trade["date"] == label["execution_date"])
            expected = equities[label["end_date"]] / (equities[label["execution_date"]] + entry_fees) - 1
            self.assertAlmostEqual(event["payload"]["net_return"], expected)
            self.assertGreaterEqual(utc_timestamp(event["event_at"]), utc_timestamp(label["mature_at"]))
            matured += 1
        self.assertGreater(matured, 0)
        self.assertEqual(len(created) - matured, len(candidates) * (self.config["selector"]["horizon_days"] + 1))

    def test_bad_research_protocols_are_rejected(self):
        cases = [{"max_attempts": -1}, {"attempts_per_cycle": 0}, {"activation_delay_days": 0}, {"train_days": 1},
                 {"shadow_delay_days": 0}, {"confirmation_timeout_days": 1}, {"max_drawdown": 0},
                 {"uncertainty_penalty": float("nan")}, {"min_parent_excess": -1}, {"unknown": True},
                 {"candidate_plan": []}, {"candidate_plan": [self.spec, self.spec]},
                 {"candidate_plan": [{**self.spec, "parent": "unknown"}]},
                 {"candidate_plan": [{**self.spec, "hypothesis": ""}]},
                 {"candidate_plan": [{**self.spec, "lookback_days": 0}]}]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ResearchConfig.parse({**self.research, **extra}, STRATEGIES)

    def test_research_journal_is_atomic_idempotent_and_cannot_be_overwritten(self):
        data = self.root / "data/demo_prices.csv"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "research.sqlite3"
            identifier, results = run_research_mix_experiment(data, self.config, database)
            adaptive = next(r for r in results if r.strategy == "adaptive_mix")
            with closing(setup_research_db(database)) as db:
                before = list(db.iterdump())
                config = json.loads(db.execute("SELECT config_json FROM experiments").fetchone()[0])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM research_events").fetchone()[0], len(adaptive.research_events))
            self.assertEqual((identifier, results), run_research_mix_experiment(data, self.config, database))
            panel = load_price_csv(data)
            changed = replace(adaptive, research_events=({**adaptive.research_events[0], "payload": {"changed": True}},)
                              + adaptive.research_events[1:])
            altered = [changed if r.strategy == "adaptive_mix" else r for r in results]
            with closing(setup_research_db(database)) as db:
                self.assertEqual(list(db.iterdump()), before)
                with self.assertRaisesRegex(ValueError, "immutable"):
                    save_experiment(db, identifier, panel, config, altered)
                self.assertEqual(list(db.iterdump()), before)
            broken = replace(adaptive, research_events=({**adaptive.research_events[0], "event_at": None},))
            db = setup_research_db(":memory:")
            try:
                before = list(db.iterdump())
                with self.assertRaises(sqlite3.IntegrityError):
                    save_experiment(db, "broken", panel, config,
                                    [broken if r.strategy == "adaptive_mix" else r for r in results])
                self.assertEqual(list(db.iterdump()), before)
            finally:
                db.close()

    def test_cli_research_mode_reports_attempts_and_preserves_baseline_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({**self.config, "data": str(self.root / "data/demo_prices.csv")}), encoding="utf-8")
            command = [sys.executable, str(self.root / "research.py"), "--research", "--config", str(path),
                       "--database", str(Path(directory) / "run.sqlite3"), "--json"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("selector", payload)
            self.assertIn("research", payload)
            self.assertLessEqual(payload["research"]["hypothesis_registered"], 1)
            self.assertEqual(next(r for r in payload["results"] if r["strategy"] == "dynamic_mix")["role"], "benchmark")
            self.assertEqual(next(r for r in payload["results"] if r["strategy"] == "adaptive_mix")["role"], "portfolio")
            invalid = subprocess.run(command + ["--selector"], capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)


if __name__ == "__main__":
    unittest.main()
