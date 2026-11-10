"""Run deterministic research with a UTC historical clock and delayed orders."""

from __future__ import annotations

import argparse
import json
import sys

from research_system import STRATEGIES, run_experiment, run_fixed_mix_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", help="rectangular date,asset,close CSV; defaults to configured data")
    parser.add_argument("--config", default="research_config.json")
    parser.add_argument("--database", help="override the SQLite output path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strategy", choices=["all", *STRATEGIES], default="all")
    mode.add_argument("--portfolio", action="store_true", help="fixed-budget portfolio with all strategy shadows")
    parser.add_argument("--budget", action="append", default=[], metavar="STRATEGY=WEIGHT",
                        help="repeat to set all portfolio strategy budgets; requires --portfolio")
    parser.add_argument("--start", dest="start_at", help="inclusive first release time, with timezone")
    parser.add_argument("--end", dest="end_at", help="inclusive final release time, with timezone")
    parser.add_argument("--data-delay-seconds", type=float, help="daily-close availability delay, below 86400")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable results")
    args = parser.parse_args()
    if args.budget and not args.portfolio:
        parser.error("--budget requires --portfolio")
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
        if args.portfolio:
            identifier, results = run_fixed_mix_experiment(data_path, config, database)
        else:
            identifier, results = run_experiment(data_path, config, strategies, database)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"experiment_id": identifier,
                          "results": [{"strategy": item.strategy, "role": item.role, **item.metrics,
                                       "pending_orders": sum(event["event"] == "pending" for event in item.order_events)}
                                      for item in results]}, indent=2))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
