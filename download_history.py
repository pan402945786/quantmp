"""Download BTC and SOL daily history for quantmp."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, date, timedelta, timezone

from market_data import download_panel


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "SOLUSDT"])
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=(datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
                        help="exclusive UTC date; defaults to tomorrow so today's completed candle is included")
    parser.add_argument("--output", default="data/btc_sol_daily.csv")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    try:
        metadata = download_panel(args.symbols, args.start, args.end, args.output, args.timeout)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
