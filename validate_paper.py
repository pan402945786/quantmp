"""Audit a declared, budget-matched historical diagnostic without changing live runs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from frozen_replay import run_frozen
from historical_replay import utc_timestamp
from replay_artifacts import canonical_json, digest, now_utc, price_identity, write_immutable
from replay_report import build_report, write_report
from research_system import load_price_csv


def scenarios(base: dict, start: str, end: str) -> list[dict]:
    """Declare every comparison before observing any of its results."""
    if utc_timestamp(start) >= utc_timestamp(end):
        raise ValueError("start must precede end")
    cases = []
    for cost in (10, 25, 50):
        for name, lookback, strategy, mode in (
            ("momentum_5d_50", 5, "momentum", "portfolio"),
            ("momentum_20d_50", 20, "momentum", "portfolio"),
            ("equal_weight_50", 20, "equal_weight", "portfolio"),
            ("adaptive_20d_50", 20, "momentum", "research"),
        ):
            cfg = copy.deepcopy(base)
            cfg.update(start_at=start, end_at=end, lookback_days=lookback,
                       transaction_cost_bps=cost,
                       strategy_budgets={strategy: 0.5, "cash": 0.5})
            cfg.setdefault("selector", {})["strategy_budget"] = 0.5
            cases.append({"name": f"{name}_cost{cost}", "mode": mode,
                          "account": "adaptive_mix" if mode == "research" else "fixed_mix",
                          "config": cfg})
    cfg = copy.deepcopy(base)
    cfg.update(start_at=start, end_at=end, lookback_days=20,
               transaction_cost_bps=10, strategy_budgets={"cash": 1.0})
    cases.append({"name": "cash_cost10", "mode": "portfolio", "account": "fixed_mix", "config": cfg})
    return cases


def continuous_slice(result, start: str, end: str) -> dict:
    """Measure [start, end) from the preceding equity, preserving existing holdings."""
    lower, upper = utc_timestamp(start), utc_timestamp(end)
    selected = [(i, row) for i, row in enumerate(result.portfolio)
                if lower <= utc_timestamp(row["as_of"]) < upper]
    if not selected:
        raise ValueError("segment contains no equity points")
    first = selected[0][0]
    anchor = result.portfolio[max(0, first - 1)]["equity"]
    peak, drawdown = anchor, 0.0
    for _, row in selected:
        peak = max(peak, row["equity"])
        drawdown = min(drawdown, row["equity"] / peak - 1)
    return {"start_at": start, "end_at_exclusive": end, "equity_points": len(selected),
            "starting_equity": anchor, "ending_equity": selected[-1][1]["equity"],
            "net_return": selected[-1][1]["equity"] / anchor - 1, "max_drawdown": drawdown}


def validate_case(panel, case: dict):
    checkpoint, results = run_frozen(panel, case["config"], case["mode"])
    report = build_report(checkpoint, panel, results)
    if not report["audit"]["passed"]:
        raise ValueError(f"{case['name']}: independent audit failed: {report['audit']['examples']}")
    result = next(row for row in results if row.strategy == case["account"])
    expected_start = utc_timestamp(case["config"]["start_at"])
    if utc_timestamp(result.portfolio[0]["as_of"]) != expected_start:
        raise ValueError("common start unavailable: provide sufficient daily warmup for every scenario")
    account = next(row for row in report["accounts"] if row["account"] == case["account"])
    summary = {"name": case["name"], "account": result.strategy,
               "transaction_cost_bps": case["config"]["transaction_cost_bps"],
               "run_id": report["run_id"], "checkpoint_hash": report["checkpoint_hash"],
               "start_at": account["start_at"], "end_at": account["end_at"],
               "metrics": result.metrics, "audit": report["audit"],
               "max_underwater_days": account["max_underwater_days"],
               "max_days_below_initial_capital": account["max_days_below_initial_capital"],
               "research_attempts": report["research_attempts"]}
    return summary, report, results, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="paper_v2_config.json")
    parser.add_argument("--data")
    parser.add_argument("--start", default="2021-07-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="inclusive replay availability bound")
    parser.add_argument("--output", required=True, help="new directory for immutable artifacts")
    args = parser.parse_args()
    try:
        base = json.loads(Path(args.config).read_text(encoding="utf-8"))
        panel = load_price_csv(args.data or base["data"])
        plan = {"schema": "quantmp-paper-validation-v1", "created_at": now_utc().isoformat(),
                "data": price_identity(panel), "raw_data_hash": panel.content_hash,
                "validator_sha256": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
                "cases": scenarios(base, args.start, args.end),
                "interpretation": "Post-hoc historical diagnostic. Equal strategy budgets do not imply equal realized volatility or exposure. No parameter promotion or independent profitability confirmation."}
        root = Path(args.output)
        if root.exists() and any(root.iterdir()):
            raise ValueError("output must be a new or empty directory; keep previous trials")
        write_immutable(root / "protocol.json", canonical_json(plan) + "\n")
        summary = {"protocol_hash": digest(plan), "protocol": plan, "cases": []}
        for case in plan["cases"]:
            item, report, results, result = validate_case(panel, case)
            item["reports"] = write_report(root / case["name"], report, results)
            # Segments share the full replay's state; neither holdings nor feedback are reset.
            boundaries = [args.start, "2023-01-01T00:00:00Z", "2025-01-01T00:00:00Z"]
            boundaries = [t for t in boundaries if utc_timestamp(args.start) <= utc_timestamp(t) <= utc_timestamp(args.end)]
            from datetime import timedelta
            boundaries.append((utc_timestamp(args.end) + timedelta(microseconds=1)).isoformat())
            item["segments"] = [continuous_slice(result, left, right)
                                for left, right in zip(boundaries, boundaries[1:])]
            summary["cases"].append(item)
            write_immutable(root / case["name"] / "summary.json", canonical_json(item) + "\n")
            print(json.dumps({"name": case["name"], "net_return": result.metrics["total_return"],
                              "max_drawdown": result.metrics["max_drawdown"], "audit_passed": True}), flush=True)
        clocks = {(row["start_at"], row["end_at"]) for row in summary["cases"]}
        if len(clocks) != 1:
            raise ValueError("comparison clocks do not match")
        write_immutable(root / "summary.json", canonical_json(summary) + "\n")
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
