import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import test_strategy_research as research_fixture
from frozen_replay import persist_frozen, run_frozen, save_checkpoint
from historical_replay import daily_close
from replay_artifacts import (
    canonical_json, decode_state, read_checkpoint, seal_checkpoint, validate_checkpoint, write_immutable,
)
from replay_report import build_report, checkpoint_results, write_report
from research_system import STRATEGIES, PricePanel


class FrozenReplayTests(unittest.TestCase):
    def setUp(self):
        fixture = research_fixture.StrategyResearchTests()
        fixture.setUp()
        self.fixture = fixture
        self.panel = fixture.panel
        self.config = fixture.config

    def test_all_modes_resume_exactly_with_pending_orders_and_feedback(self):
        for mode in ("standalone", "portfolio", "selector", "research"):
            with self.subTest(mode=mode):
                full_checkpoint, full = run_frozen(self.panel, self.config, mode)
                partial, _ = run_frozen(self.panel, self.config, mode, until="2025-01-25T00:00:00Z")
                raw = json.loads(canonical_json(partial))
                resumed, result = run_frozen(self.panel, checkpoint=raw)
                self.assertEqual(full, result)
                self.assertEqual(resumed["parent_hash"], partial["checkpoint_hash"])
                self.assertEqual(resumed["manifest"], partial["manifest"])
                self.assertEqual(checkpoint_results(resumed), full)
                self.assertTrue(build_report(resumed, self.panel, result)["audit"]["passed"])
                state = decode_state(partial["state"])
                self.assertTrue(all(ledger.pending is not None for ledger in state["accounts"]))
                if mode in {"selector", "research"}:
                    self.assertTrue(state["feedback"]._pending)

    def test_candidate_freeze_confirmation_activation_and_retirement_survive_restart(self):
        prices = {on: {asset: value * (0.2 if i >= 48 else 1) for asset, value in self.panel.prices[on].items()}
                  for i, on in enumerate(self.panel.dates)}
        panel = replace(self.panel, prices=prices)
        _, full = run_frozen(panel, self.config)
        adaptive = next(r for r in full if r.strategy == "adaptive_mix")
        for kind in ("candidate_frozen", "candidate_confirmed", "candidate_activated", "candidate_exit_scheduled", "candidate_retired"):
            with self.subTest(stage=kind):
                event = next(e for e in adaptive.research_events if e["event_type"] == kind)
                checkpoint, _ = run_frozen(panel, self.config, until=event["event_at"])
                restored, results = run_frozen(panel, checkpoint=json.loads(canonical_json(checkpoint)))
                self.assertEqual(results, full)
                self.assertTrue(build_report(restored, panel, results)["audit"]["passed"])
                state = decode_state(checkpoint["state"])
                for active in state["research"]._active.values():
                    self.assertIs(active, state["research"]._states[active.version.version_id])

    def test_resuming_does_not_repeat_past_strategy_calls(self):
        checkpoint, _ = run_frozen(self.panel, self.config, "standalone", ["momentum", "cash"],
                                   until="2025-01-25T00:00:00Z")
        original = dict(STRATEGIES)
        calls = []

        def wrapped(name):
            def target(on, history, config):
                calls.append((name, on))
                return original[name](on, history, config)
            return target

        with patch.dict(STRATEGIES, {name: wrapped(name) for name in ("momentum", "cash")}):
            _, results = run_frozen(self.panel, checkpoint=checkpoint)
        self.assertTrue(calls)
        self.assertTrue(all(on > checkpoint["last_date"] for _, on in calls))
        self.assertEqual(len(calls), 2 * len([on for on in self.panel.dates if on > checkpoint["last_date"]]))
        self.assertEqual(len(results[0].portfolio), len(self.panel.dates) - self.config["lookback_days"])

    def test_append_only_data_continuation_matches_a_complete_replay(self):
        prefix = replace(self.panel, dates=self.panel.dates[:30], content_hash="prefix")
        checkpoint, _ = run_frozen(prefix, self.config)
        continued, actual = run_frozen(self.panel, checkpoint=checkpoint)
        _, expected = run_frozen(self.panel, self.config)
        self.assertEqual(actual, expected)
        self.assertGreater(continued["known_data"]["days"], checkpoint["known_data"]["days"])
        self.assertEqual(continued["manifest"]["origin_data"], checkpoint["known_data"])

    def test_rewrites_truncation_asset_changes_and_missing_append_dates_are_rejected(self):
        checkpoint, _ = run_frozen(self.panel, self.config, until="2025-01-25T00:00:00Z")
        for on in (self.panel.dates[0], self.panel.dates[-1]):
            prices = {day: dict(row) for day, row in self.panel.prices.items()}
            prices[on]["AAA"] *= 1.01
            with self.subTest(changed=on), self.assertRaisesRegex(ValueError, "previously known prices"):
                run_frozen(replace(self.panel, prices=prices), checkpoint=checkpoint)
        with self.assertRaisesRegex(ValueError, "previously known prices"):
            run_frozen(replace(self.panel, dates=self.panel.dates[:-1]), checkpoint=checkpoint)
        prices = {on: {**row, "CCC": 1.0} for on, row in self.panel.prices.items()}
        with self.assertRaisesRegex(ValueError, "previously known prices"):
            run_frozen(replace(self.panel, assets=(*self.panel.assets, "CCC"), prices=prices), checkpoint=checkpoint)
        end = (date.fromisoformat(self.panel.dates[-1]) + timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, "consecutive daily"):
            run_frozen(replace(self.panel, dates=self.panel.dates + (end,),
                               prices={**self.panel.prices, end: self.panel.prices[self.panel.dates[-1]]}), checkpoint=checkpoint)

    def test_parameters_modes_code_runtime_and_clock_are_frozen(self):
        checkpoint, _ = run_frozen(self.panel, self.config, until="2025-01-25T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "parameters changed"):
            run_frozen(self.panel, {**self.config, "max_weight": 0.5}, checkpoint=checkpoint)
        with self.assertRaisesRegex(ValueError, "mode changed"):
            run_frozen(self.panel, mode="portfolio", checkpoint=checkpoint)
        with self.assertRaisesRegex(ValueError, "strategy library changed"):
            run_frozen(self.panel, strategies=["cash"], checkpoint=checkpoint)
        with patch("replay_artifacts.code_identity", return_value={"hash": "different"}):
            with self.assertRaisesRegex(ValueError, "engine code changed"):
                run_frozen(self.panel, checkpoint=checkpoint)
        with patch("replay_artifacts.runtime_identity", return_value={"python": "different"}):
            with self.assertRaisesRegex(ValueError, "runtime changed"):
                run_frozen(self.panel, checkpoint=checkpoint)
        with self.assertRaisesRegex(ValueError, "backwards"):
            run_frozen(self.panel, checkpoint=checkpoint, until="2025-01-20T00:00:00Z")

    def test_noop_resume_does_not_change_checkpoint_or_append_false_events(self):
        checkpoint, results = run_frozen(self.panel, self.config)
        same, again = run_frozen(self.panel, checkpoint=checkpoint)
        self.assertEqual(same, checkpoint)
        self.assertEqual(again, results)
        self.assertEqual(again, checkpoint_results(checkpoint))

    def test_requested_future_stop_is_capped_by_the_actual_availability_clock(self):
        wall_clock = daily_close(self.panel.dates[18]) + timedelta(hours=12)
        with patch("frozen_replay.now_utc", return_value=wall_clock):
            checkpoint, results = run_frozen(self.panel, self.config, "standalone", ["cash"],
                                              until="2025-02-01T00:00:00Z")
        self.assertEqual(checkpoint["last_date"], self.panel.dates[18])
        self.assertTrue(all(daily_close(row["date"]) <= wall_clock for row in results[0].portfolio))
        self.assertLess(len(results[0].portfolio), checkpoint["known_data"]["days"])

    def test_checkpoint_digest_and_closed_codec_reject_corruption_and_unknown_classes(self):
        checkpoint, _ = run_frozen(self.panel, self.config, until="2025-01-25T00:00:00Z")
        corrupt = copy.deepcopy(checkpoint)
        corrupt["last_date"] = "2025-01-01"
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            validate_checkpoint(corrupt)
        with self.assertRaisesRegex(ValueError, "unknown checkpoint type"):
            decode_state({"type": "manager", "class": "os.system", "value": None})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            save_checkpoint(path, checkpoint)
            self.assertEqual(read_checkpoint(path), checkpoint)
            self.assertNotIn('"class":"PricePanel"', canonical_json(checkpoint["state"]))
            self.assertNotIn('"_rows"', canonical_json(checkpoint["state"]))
            path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON"):
                read_checkpoint(path)

    def test_artifacts_are_immutable_and_failed_publication_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            write_immutable(path, "one")
            write_immutable(path, "one")
            with self.assertRaisesRegex(ValueError, "immutable"):
                write_immutable(path, "two")
            self.assertEqual(path.read_text(encoding="utf-8"), "one")
            other = Path(directory) / "failed.json"
            with patch("replay_artifacts.os.link", side_effect=OSError("publish failed")):
                with self.assertRaises(OSError):
                    write_immutable(other, "complete")
            self.assertFalse(other.exists())
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["artifact.json"])

    def test_database_revisions_are_append_only_and_cannot_silently_fork(self):
        checkpoint, results = run_frozen(self.panel, self.config, until="2025-01-15T00:00:00Z")
        first, first_results = run_frozen(self.panel, checkpoint=checkpoint, until="2025-01-25T00:00:00Z")
        sibling, sibling_results = run_frozen(self.panel, checkpoint=checkpoint, until="2025-02-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "frozen.sqlite3"
            with self.assertRaisesRegex(ValueError, "parent checkpoint"):
                persist_frozen(database, first, self.panel, first_results)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM frozen_runs").fetchone()[0], 0)
            identifier = persist_frozen(database, checkpoint, self.panel, results)
            persist_frozen(database, first, self.panel, first_results)
            with closing(sqlite3.connect(database)) as db:
                before = list(db.iterdump())
            self.assertEqual(identifier, persist_frozen(database, checkpoint, self.panel, results))
            with self.assertRaisesRegex(ValueError, "already has a continuation"):
                persist_frozen(database, sibling, self.panel, sibling_results)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(before, list(db.iterdump()))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM frozen_checkpoints").fetchone()[0], 2)

    def test_monthly_returns_duration_and_accounting_contributions_have_known_values(self):
        dates = tuple(f"2025-01-{day:02d}" for day in range(1, 8))
        prices = [100, 100, 100, 80, 70, 100, 90]
        panel = PricePanel(dates, ("AAA",), {on: {"AAA": value} for on, value in zip(dates, prices)}, "test", "test")
        config = {"initial_capital": 100, "lookback_days": 1, "max_weight": 1, "transaction_cost_bps": 0}
        checkpoint, results = run_frozen(panel, config, "standalone", ["equal_weight"])
        report = build_report(checkpoint, panel, results)
        account = report["accounts"][0]
        self.assertTrue(report["audit"]["passed"])
        self.assertAlmostEqual(account["monthly"][0]["net_return"], -0.1)
        self.assertEqual(account["max_underwater_days"], 2)
        self.assertEqual(account["max_days_below_initial_capital"], 2)
        self.assertAlmostEqual(account["asset_contributions"]["AAA"]["net_pnl"], -10)
        self.assertAlmostEqual(sum(row["net_pnl"] for row in account["version_contributions"].values()), -10)

    def test_independent_audit_detects_bad_fees_metrics_and_execution_times(self):
        checkpoint, results = run_frozen(self.panel, self.config)
        actual = next(r for r in results if r.strategy == "adaptive_mix")
        changes = [replace(actual, metrics={**actual.metrics, "ending_equity": 1}),
                   replace(actual, trades=({**actual.trades[0], "fee": actual.trades[0]["fee"] + 1},) + actual.trades[1:]),
                   replace(actual, predictions=({**actual.predictions[0], "execution_at": actual.predictions[0]["decision_at"]},)
                           + actual.predictions[1:])]
        for changed in changes:
            with self.subTest(change=changed.metrics["ending_equity"]):
                report = build_report(checkpoint, self.panel, [changed if r.strategy == actual.strategy else r for r in results])
                self.assertFalse(report["audit"]["passed"])
                self.assertGreater(report["audit"]["failure_count"], 0)

    def test_backdated_appends_are_not_claimed_as_post_freeze_confirmation(self):
        prefix = replace(self.panel, dates=self.panel.dates[:20], content_hash="prefix")
        checkpoint, _ = run_frozen(prefix, self.config)
        resumed, results = run_frozen(replace(self.panel, dates=self.panel.dates[:25]), checkpoint=checkpoint)
        confirmation = build_report(resumed, replace(self.panel, dates=self.panel.dates[:25]), results)["confirmation"]
        self.assertEqual(confirmation["appended_rows_processed"], 5)
        self.assertEqual(confirmation["eligible_post_freeze_rows"], 0)
        self.assertEqual(confirmation["status"], "awaiting_post_freeze_rows")
        self.assertFalse(confirmation["independent_profitability_confirmed"])

    def test_only_new_rows_available_after_the_wall_clock_freeze_are_separately_reported(self):
        prefix = replace(self.panel, dates=self.panel.dates[:20], content_hash="prefix")
        frozen_at = daily_close(prefix.dates[-1]) + timedelta(hours=12)
        with patch("frozen_replay.now_utc", return_value=frozen_at):
            checkpoint, _ = run_frozen(prefix, self.config)
        extended = replace(self.panel, dates=self.panel.dates[:25])
        with patch("frozen_replay.now_utc", return_value=frozen_at + timedelta(days=5)):
            resumed, results = run_frozen(extended, checkpoint=checkpoint)
        confirmation = build_report(resumed, extended, results)["confirmation"]
        self.assertEqual(confirmation["eligible_post_freeze_rows"], 5)
        self.assertEqual(confirmation["status"], "post_freeze_rows_available_under_protocol")
        self.assertEqual(len(confirmation["accounts"]), len(results))
        self.assertFalse(confirmation["independent_profitability_confirmed"])

    def test_reports_export_full_equity_csv_and_immutable_json_and_markdown(self):
        checkpoint, results = run_frozen(self.panel, self.config)
        report = build_report(checkpoint, self.panel, results)
        with tempfile.TemporaryDirectory() as directory:
            paths = write_report(directory, report, results)
            self.assertEqual(json.loads(Path(paths["json"]).read_text(encoding="utf-8")), report)
            self.assertIn("新增数据确认状态", Path(paths["markdown"]).read_text(encoding="utf-8"))
            lines = Path(paths["equity_csv"]).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1 + sum(len(r.portfolio) for r in results))
            self.assertEqual(write_report(directory, report, results), paths)

    def test_cli_starts_resumes_and_reports_without_replaying_history(self):
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            database = directory / "frozen.sqlite3"
            first, second = directory / "first.json", directory / "second.json"
            common = ["--database", str(database), "--report-dir", str(directory / "reports")]
            command = [sys.executable, str(root / "replay.py"), "start", "--config", str(root / "research_loop_config.json"),
                       "--data", str(root / "data/demo_prices.csv"), "--until", "2025-01-12T00:00:00Z", "--checkpoint", str(first), *common]
            started = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(started.returncode, 0, started.stderr)
            archived_cli = Path(json.loads(started.stdout)["engine_archive"]) / "replay.py"
            self.assertTrue(archived_cli.is_file())
            resumed = subprocess.run([sys.executable, str(root / "replay.py"), "resume", "--from", str(first),
                                      "--checkpoint", str(second), *common], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            payload = json.loads(resumed.stdout)
            self.assertTrue(payload["audit"]["passed"])
            inspected = subprocess.run([sys.executable, str(root / "replay.py"), "report", "--from", str(second),
                                        "--report-dir", str(directory / "reports")], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["checkpoint_hash"], payload["checkpoint_hash"])
            archived_report = subprocess.run([sys.executable, str(archived_cli), "report", "--from", str(second),
                                              "--report-dir", str(directory / "reports")],
                                             capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(archived_report.returncode, 0, archived_report.stderr)


if __name__ == "__main__":
    unittest.main()
