import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from historical_replay import (
    DailyReplay, HistoryView, ScheduledTargets, UnavailableDataError, daily_close, utc_timestamp,
)
from research_system import (
    PricePanel, STRATEGIES, backtest, save_experiment, setup_research_db,
)


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        self.dates = tuple(f"2025-01-{day:02d}" for day in range(1, 9))
        self.prices = {on: {"AAA": 100.0 + index * 5, "BBB": 100.0 - index * 3}
                       for index, on in enumerate(self.dates)}
        self.panel = PricePanel(self.dates, ("AAA", "BBB"), self.prices, "full-input.csv", "full-file-hash")
        self.config = {"initial_capital": 1000, "lookback_days": 2,
                       "max_weight": 0.6, "transaction_cost_bps": 10}

    def test_bars_release_only_after_close_and_configured_delay(self):
        replay = DailyReplay(self.dates, self.prices, 10)
        before = replay.advance_to("2025-01-01T12:00:00+00:00")
        self.assertEqual((before.dates, before.assets), ((), ()))
        for clock in ["2025-01-02T00:00:00+00:00", "2025-01-02T00:00:09.999999+00:00"]:
            view = replay.advance_to(clock)
            with self.assertRaises(UnavailableDataError):
                view.price("2025-01-01", "AAA")
        view = replay.advance_to("2025-01-02T08:00:10+08:00")
        self.assertEqual(view.dates, ("2025-01-01",))
        self.assertEqual(view.price("2025-01-01", "AAA"), 100)
        timing = view.timing("2025-01-01")
        self.assertEqual(timing.event_at, utc_timestamp("2025-01-02T00:00:00Z"))
        self.assertEqual(timing.available_at, utc_timestamp("2025-01-02T00:00:10Z"))
        self.assertIsNone(timing.published_at)
        self.assertIsNone(timing.collected_at)

    def test_snapshots_are_detached_immutable_and_do_not_expose_future_metadata(self):
        replay = DailyReplay(self.dates, self.prices)
        old = replay.advance_to("2025-01-04T00:00:00Z")
        with self.assertRaises(TypeError):
            old.prices["2025-01-01"]["AAA"] = 999
        with self.assertRaises(TypeError):
            old.prices["2025-01-09"] = {"AAA": 999}
        with self.assertRaises(TypeError):
            old.timings["2025-01-01"] = None
        with self.assertRaises(FrozenInstanceError):
            old.as_of = daily_close("2025-01-08")
        with self.assertRaises(FrozenInstanceError):
            old.timing("2025-01-01").available_at = old.as_of
        self.prices["2025-01-01"]["AAA"] = 777
        self.prices["2025-01-08"]["AAA"] = 888
        later = replay.advance_to("2025-01-09T00:00:00Z")
        self.assertEqual(old.dates, self.dates[:3])
        self.assertEqual(old.price("2025-01-01", "AAA"), 100)
        self.assertEqual(later.price("2025-01-08", "AAA"), 135)
        for name in ("source", "_panel", "_replay", "_rows", "__dict__"):
            self.assertFalse(hasattr(old, name))
        with self.assertRaises(UnavailableDataError):
            old.price("2025-01-08", "AAA")
        with self.assertRaises(UnavailableDataError):
            old.timing("2025-01-08")
        with self.assertRaises(IndexError):
            _ = old.dates[len(old.dates)]

    def test_snapshot_digest_and_asset_catalog_use_only_released_rows(self):
        changed = {on: dict(row) for on, row in self.prices.items()}
        changed["2025-01-08"] = {"AAA": 1e9, "FUTURE_ONLY": 1}
        variants = [(self.dates, self.prices), (self.dates[:4], self.prices), (self.dates, changed)]
        views = [DailyReplay(dates, prices).advance_to("2025-01-05T00:00:00Z") for dates, prices in variants]
        self.assertEqual(views[0], views[1])
        self.assertEqual(views[0], views[2])
        self.assertNotEqual(views[0].content_hash, self.panel.content_hash)
        self.assertNotIn("FUTURE_ONLY", views[0].assets)

    def test_clock_requires_timezone_and_cannot_move_backwards(self):
        replay = DailyReplay(self.dates, self.prices)
        with self.assertRaisesRegex(ValueError, "timezone"):
            replay.advance_to("2025-01-04T00:00:00")
        first = replay.advance_to("2025-01-04T00:00:00Z")
        self.assertEqual(first, replay.advance_to(first.as_of))
        with self.assertRaisesRegex(ValueError, "backwards"):
            replay.advance_to("2025-01-03T23:59:59Z")

    def test_strategy_receives_only_visible_history_and_frozen_parameters(self):
        calls = []

        def probe(on, history, config):
            self.assertIsInstance(history, HistoryView)
            self.assertEqual(history.dates[-1], on)
            self.assertEqual(history.as_of, daily_close(on))
            self.assertTrue(all(timing.available_at <= history.as_of for timing in history.timings.values()))
            self.assertNotIn("data", config)
            self.assertNotIn("end_at", config)
            self.assertNotIn("untrusted_future_hint", config)
            with self.assertRaises(TypeError):
                config["lookback_days"] = 999
            future = (utc_timestamp(history.as_of) + timedelta(days=1)).date().isoformat()
            with self.assertRaises(UnavailableDataError):
                history.price(future, "AAA")
            calls.append(history)
            return {"AAA": 0.6}

        with patch.dict(STRATEGIES, {"probe": probe}):
            result = backtest(self.panel, {**self.config, "end_at": "2025-01-07T00:00:00Z",
                                          "untrusted_future_hint": "must not reach strategy"}, "probe")
        self.assertEqual(len(calls), len(result.predictions))
        self.assertEqual(calls[0].dates, self.dates[:3])
        self.assertEqual(calls[0].content_hash, result.predictions[0]["history_hash"])
        self.assertEqual(calls[0].dates, self.dates[:3])  # Later ticks cannot grow an old snapshot.

    def test_mutating_appending_or_truncating_future_preserves_decision_prefix(self):
        cutoff = self.dates[4]
        changed = {on: {asset: value * (100 if on > cutoff else 1) for asset, value in row.items()}
                   for on, row in self.prices.items()}
        appended = {**self.prices, "2025-01-09": {"AAA": 0.01, "BBB": 1e7}}
        variants = [replace(self.panel, prices=changed, content_hash="changed"),
                    replace(self.panel, dates=self.dates + ("2025-01-09",), prices=appended, content_hash="appended"),
                    replace(self.panel, dates=self.dates[:5], content_hash="truncated")]
        for strategy in STRATEGIES:
            baseline = backtest(self.panel, self.config, strategy)
            for panel in variants:
                with self.subTest(strategy=strategy, variant=panel.content_hash):
                    actual = backtest(panel, self.config, strategy)
                    for name, key in [("predictions", "decision_date"), ("trades", "date"), ("portfolio", "date")]:
                        self.assertEqual([row for row in getattr(baseline, name) if row[key] <= cutoff],
                                         [row for row in getattr(actual, name) if row[key] <= cutoff])

    def test_single_decision_does_not_require_any_future_bar(self):
        panel = replace(self.panel, dates=self.dates[:3])
        result = backtest(panel, self.config, "equal_weight")
        self.assertEqual(len(result.predictions), 1)
        self.assertEqual(result.trades, ())
        self.assertEqual(result.metrics["ending_equity"], 1000)
        self.assertEqual([row["event"] for row in result.order_events], ["submitted", "pending"])

    def test_orders_execute_after_submission_with_separate_processing_time(self):
        result = backtest(self.panel, {**self.config, "data_delay_seconds": 10}, "equal_weight")
        submitted = {row["decision_date"]: row for row in result.order_events if row["event"] == "submitted"}
        fills = [row for row in result.order_events if row["event"] == "filled"]
        self.assertEqual(len(fills), len(result.predictions) - 1)
        for fill in fills:
            self.assertLess(utc_timestamp(submitted[fill["decision_date"]]["event_at"]), utc_timestamp(fill["event_at"]))
            self.assertEqual(utc_timestamp(fill["event_at"]), daily_close(fill["execution_date"]))
            self.assertEqual(utc_timestamp(fill["processed_at"]), utc_timestamp(fill["event_at"]) + timedelta(seconds=10))
        self.assertEqual(result.trades[0]["date"], "2025-01-04")
        self.assertEqual(result.order_events[-1]["event"], "pending")
        self.assertGreater(utc_timestamp(result.order_events[-1]["execution_at"]),
                           utc_timestamp(result.order_events[-1]["processed_at"]))

    def test_scheduled_targets_freeze_weights_and_reject_early_execution(self):
        targets = {"AAA": 0.6}
        order = ScheduledTargets("2025-01-03", daily_close("2025-01-03"), "2025-01-04", targets)
        targets["AAA"] = 0
        self.assertEqual(order.targets["AAA"], 0.6)
        with self.assertRaises(TypeError):
            order.targets["AAA"] = 0
        with self.assertRaisesRegex(ValueError, "before their execution"):
            order.event("filled", order.submitted_at)
        with self.assertRaisesRegex(ValueError, "submitted before"):
            ScheduledTargets("2025-01-03", daily_close("2025-01-03"), "2025-01-03", targets)

    def test_start_uses_warmup_and_end_does_not_release_or_fill_future(self):
        config = {**self.config, "start_at": "2025-01-05T12:00:00Z", "end_at": "2025-01-07T23:59:59Z"}
        result = backtest(self.panel, config, "equal_weight")
        self.assertEqual(result.portfolio[0]["date"], "2025-01-05")
        self.assertEqual(result.portfolio[0]["equity"], 1000)
        self.assertEqual(result.predictions[0]["visible_rows"], 5)
        self.assertEqual(result.portfolio[-1]["date"], "2025-01-06")
        self.assertEqual(result.order_events[-1]["processed_at"], "2025-01-07T00:00:00+00:00")
        self.assertEqual(result.order_events[-1]["execution_date"], "2025-01-07")
        self.assertTrue(all(utc_timestamp(row["processed_at"]) <= utc_timestamp(config["end_at"])
                            for row in result.order_events))
        truncated = backtest(replace(self.panel, dates=self.dates[:6]), config, "equal_weight")
        self.assertEqual(result, truncated)

    def test_invalid_intervals_and_delays_are_rejected(self):
        cases = [{"start_at": "2025-01-08T00:00:00Z", "end_at": "2025-01-04T00:00:00Z"},
                 {"end_at": "2025-01-02T00:00:00Z"}, {"start_at": "2025-02-01T00:00:00Z"},
                 {"start_at": "2025-01-05"}]
        cases += [{"data_delay_seconds": value} for value in [-1, 86400, float("inf"), float("nan")]]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                backtest(self.panel, {**self.config, **extra}, "cash")

    def test_replay_audit_is_saved_immutably_and_late_write_failures_roll_back(self):
        result = backtest(self.panel, self.config, "equal_weight")
        db = setup_research_db(":memory:")
        try:
            broken = replace(result, order_events=result.order_events + result.order_events[:1])
            with self.assertRaises(sqlite3.IntegrityError):
                save_experiment(db, "replay", self.panel, self.config, [broken])
            for table in ("experiments", "predictions", "trades", "portfolio_daily", "metrics", "decision_audit", "order_events"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            save_experiment(db, "replay", self.panel, self.config, [result])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0], len(result.predictions))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_events WHERE event='pending'").fetchone()[0], 1)
            before = list(db.iterdump())
            altered = replace(result, predictions=({**result.predictions[0], "history_hash": "other"},) + result.predictions[1:])
            with self.assertRaisesRegex(ValueError, "immutable"):
                save_experiment(db, "replay", self.panel, self.config, [altered])
            self.assertEqual(list(db.iterdump()), before)
        finally:
            db.close()

    def test_cli_supports_timestamp_bounds_and_reports_pending_orders(self):
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replay.sqlite3"
            args = [sys.executable, str(root / "research.py"), "--data", str(root / "data/demo_prices.csv"),
                    "--config", str(root / "research_config.json"), "--database", str(database),
                    "--strategy", "equal_weight", "--start", "2025-01-09T00:00:00Z",
                    "--end", "2025-01-12T00:00:00Z", "--data-delay-seconds", "0", "--json"]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["results"][0]["pending_orders"], 1)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT MAX(decision_at) FROM decision_audit").fetchone()[0],
                                 "2025-01-12T00:00:00+00:00")
            args[args.index("--end") + 1] = "2025-01-12"
            invalid = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("timezone", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
