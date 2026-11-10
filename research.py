"""Run deterministic research with a UTC historical clock and delayed orders."""

from __future__ import annotations

import argparse
import json
import sys

from research_system import (
    STRATEGIES, run_dynamic_mix_experiment, run_experiment, run_fixed_mix_experiment, run_research_mix_experiment,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", help="rectangular date,asset,close CSV; defaults to configured data")
    parser.add_argument("--config", default="research_config.json")
    parser.add_argument("--database", help="override the SQLite output path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strategy", choices=["all", *STRATEGIES], default="all")
    mode.add_argument("--portfolio", action="store_true", help="fixed-budget portfolio with all strategy shadows")
    mode.add_argument("--selector", action="store_true", help="mature-feedback selector with fixed and shadow controls")
    mode.add_argument("--research", action="store_true", help="bounded candidate research and prospective shadow confirmation")
    parser.add_argument("--budget", action="append", default=[], metavar="STRATEGY=WEIGHT",
                        help="repeat to set fixed benchmark budgets; requires --portfolio, --selector or --research")
    parser.add_argument("--start", dest="start_at", help="inclusive first release time, with timezone")
    parser.add_argument("--end", dest="end_at", help="inclusive final release time, with timezone")
    parser.add_argument("--data-delay-seconds", type=float, help="daily-close availability delay, below 86400")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable results")
    args = parser.parse_args()
    if args.budget and not (args.portfolio or args.selector or args.research):
        parser.error("--budget requires --portfolio, --selector or --research")
    try:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        for key in ("start_at", "end_at", "data_delay_seconds"):
            value = getattr(args, key)
            if value is not None:
                config[key] = value
        if args.budget:
            budgets = {}
            for entry in args.budget:
                if "=" not in entry:
                    raise ValueError("budget must have the form STRATEGY=WEIGHT")
                name, weight = entry.split("=", 1)
                if name in budgets:
                    raise ValueError(f"duplicate strategy budget: {name}")
                budgets[name] = float(weight)
            config["strategy_budgets"] = budgets
        strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
        data_path = args.data or config.get("data", "data/demo_prices.csv")
        database = args.database or config.get("database", "research.sqlite3")
        if args.research:
            identifier, results = run_research_mix_experiment(data_path, config, database)
        elif args.selector:
            identifier, results = run_dynamic_mix_experiment(data_path, config, database)
        elif args.portfolio:
            identifier, results = run_fixed_mix_experiment(data_path, config, database)
        else:
            identifier, results = run_experiment(data_path, config, strategies, database)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    summary = {"experiment_id": identifier,
               "results": [{"strategy": item.strategy, "role": item.role, **item.metrics,
                            "pending_orders": sum(event["event"] == "pending" for event in item.order_events)}
                           for item in results]}
    if args.selector or args.research:
        dynamic = next(item for item in results if item.strategy == "dynamic_mix")
        counts = {kind: sum(event["event_type"] == kind for event in dynamic.selector_events)
                  for kind in ("feedback_created", "feedback_matured", "research_rejected", "version_frozen", "version_activated")}
        summary["selector"] = {**counts, "pending_feedback": counts["feedback_created"] - counts["feedback_matured"],
                               "last_version": dynamic.allocations[-1]["selector_version"],
                               "last_selected_strategy": dynamic.allocations[-1]["selected_strategy"]}
    if args.research:
        adaptive = next(item for item in results if item.strategy == "adaptive_mix")
        kinds = ("hypothesis_registered", "research_attempt_completed", "research_attempt_failed", "candidate_frozen",
                 "candidate_shadow_started", "candidate_confirmed", "candidate_activated", "candidate_rejected",
                 "candidate_exit_scheduled", "candidate_retired", "research_budget_exhausted")
        counts = {kind: sum(event["event_type"] == kind for event in adaptive.research_events) for kind in kinds}
        pending_feedback = sum(event["event_type"] == "feedback_created" for event in adaptive.research_events) - sum(
            event["event_type"] == "feedback_matured" for event in adaptive.research_events)
        rejected = sum(event["event_type"] == "research_attempt_completed" and not event["payload"]["passed"]
                       for event in adaptive.research_events)
        summary["research"] = {**counts, "internal_validation_rejected": rejected, "pending_candidate_feedback": pending_feedback,
                               "effective_replacements": adaptive.allocations[-1]["candidate_versions"],
                               "last_selected_strategy": adaptive.allocations[-1]["selected_strategy"]}
    if args.as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"experiment={identifier}")
        print(f"data={data_path} database={database}")
        for item in results:
            print(f"{item.role:10} {item.strategy:18} ending={item.metrics['ending_equity']:.2f} "
                  f"return={item.metrics['total_return']:.2%} "
                  f"annualized={item.metrics['annualized_return']:.2%} "
                  f"max_drawdown={item.metrics['max_drawdown']:.2%} "
                  f"volatility={item.metrics['volatility']:.2%} "
                  f"turnover={item.metrics['turnover']:.2f} "
                  f"fees={item.metrics['total_fees']:.2f} "
                  f"cash_avg={item.metrics['average_cash_weight']:.2%} "
                  f"pending={sum(event['event'] == 'pending' for event in item.order_events)}")
        if args.selector or args.research:
            print("selector=" + json.dumps(summary["selector"], sort_keys=True))
        if args.research:
            print("research=" + json.dumps(summary["research"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
