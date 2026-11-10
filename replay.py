"""Freeze, resume and audit a quantmp historical replay without resetting funds."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from frozen_replay import MODES, persist_frozen, run_frozen, save_checkpoint
from replay_artifacts import archive_code, read_checkpoint
from replay_report import build_report, checkpoint_results, write_report
from research_system import load_price_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="freeze the current code, parameters and input data")
    start.add_argument("--config", default="research_loop_config.json")
    start.add_argument("--mode", choices=MODES, default="research")
    start.add_argument("--strategy", action="append", help="standalone strategies; repeat as needed")
    resume = commands.add_parser("resume", help="continue from a checkpoint using unchanged or appended input")
    resume.add_argument("--from", dest="source_checkpoint", required=True)
    inspect = commands.add_parser("report", help="audit a saved checkpoint without replaying decisions")
    inspect.add_argument("--from", dest="source_checkpoint", required=True)
    for command in (start, resume):
        command.add_argument("--until", help="inclusive UTC availability bound; requires timezone")
        command.add_argument("--checkpoint", required=True, help="new immutable checkpoint JSON path")
        command.add_argument("--database", default="research_frozen.sqlite3")
    for command in (start, resume, inspect):
        command.add_argument("--data", help="daily price CSV; resume defaults to the original source")
        command.add_argument("--report-dir", default="runs/reports")
    args = parser.parse_args()
    try:
        if args.command == "start":
            with open(args.config, encoding="utf-8") as handle:
                config = json.load(handle)
            panel = load_price_csv(args.data or config.get("data", "data/btc_sol_daily.csv"))
            checkpoint, results = run_frozen(panel, config, args.mode, args.strategy, until=args.until)
        else:
            previous = read_checkpoint(args.source_checkpoint)
            panel = load_price_csv(args.data or previous["data_source"])
            if args.command == "report":
                checkpoint, results = previous, checkpoint_results(previous)
            else:
                checkpoint, results = run_frozen(panel, checkpoint=previous, until=args.until)
        report = build_report(checkpoint, panel, results)
        if not report["audit"]["passed"]:
            raise ValueError("independent replay audit failed: " + "; ".join(report["audit"]["examples"][:3]))
        if args.command != "report":
            save_checkpoint(args.checkpoint, checkpoint)
            archive = archive_code(Path(args.checkpoint).parent / ("engine-" + checkpoint["manifest"]["code"]["hash"][:12]),
                                   checkpoint["manifest"]["code"])
            identifier = persist_frozen(args.database, checkpoint, panel, results)
        else:
            identifier = None
            archive = None
        paths = write_report(args.report_dir, report, results)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_id": checkpoint["manifest"]["run_id"], "checkpoint_hash": checkpoint["checkpoint_hash"],
                      "as_of": checkpoint["as_of"], "experiment_id": identifier, "audit": report["audit"],
                      "confirmation": report["confirmation"], "engine_archive": archive,
                      "reports": paths}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
