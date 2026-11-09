"""Run the phase-1 local research loop."""

from __future__ import annotations

import argparse
import json
import sys

from research_system import STRATEGIES, run_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data/demo_prices.csv", help="rectangular date,asset,close CSV")
    parser.add_argument("--config", default="research_config.json")
    parser.add_argument("--database", help="override the SQLite output path")
    parser.add_argument("--strategy", choices=["all", *STRATEGIES], default="all")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print machine-readable results")
    args = parser.parse_args()
    try:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        strategies = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
        database = args.database or config.get("database", "research.sqlite3")
        identifier, results = run_experiment(args.data, config, strategies, database)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"experiment_id": identifier,
                          "results": [{"strategy": item.strategy, **item.metrics} for item in results]}, indent=2))
    else:
        print(f"experiment={identifier}")
        print(f"data={args.data} database={database}")
        for item in results:
            print(f"{item.strategy:12} ending={item.metrics['ending_equity']:.2f} "
                  f"return={item.metrics['total_return']:.2%} "
                  f"annualized={item.metrics['annualized_return']:.2%} "
                  f"max_drawdown={item.metrics['max_drawdown']:.2%} "
                  f"volatility={item.metrics['volatility']:.2%} "
                  f"turnover={item.metrics['turnover']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
