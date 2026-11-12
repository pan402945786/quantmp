"""Accelerated daily observe / reflect / revise / advance historical research.

Corrections are bounded to a declared strategy/parameter catalog. Daily shadow
returns are completed one-day marks, not predictions or unfinished horizon labels.
The researcher receives only the current HistoryView and completed observations.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from historical_replay import DailyReplay, utc_timestamp
from replay_artifacts import (
    archive_code, canonical_json, code_identity, digest, encode_state, now_utc,
    price_identity, runtime_identity, write_immutable,
)
from replay_report import _account_report
from research_system import (
    STRATEGIES, _PortfolioLedger, _validate_config, _validate_targets,
    load_price_csv, save_experiment, setup_research_db,
)


RULE = "daily-bounded-walkforward-v1"


def resolve_config(raw: dict) -> dict:
    cfg = _validate_config(raw)
    cfg.update({key: cfg[key].isoformat() if cfg[key] else None for key in ("start_at", "end_at")})
    defaults = {"train_days": 180, "validation_days": 60, "activation_delay_days": 1,
                "uncertainty_penalty": 1.0, "min_validation_excess": 0.001,
                "max_drawdown": 0.35, "strategy_budget": 0.5,
                "initial_variant": "momentum_20d", "candidates": [
                    {"name": "momentum_5d", "strategy": "momentum", "lookback_days": 5,
                     "hypothesis": "A faster trend may respond sooner to direction changes."},
                    {"name": "momentum_20d", "strategy": "momentum", "lookback_days": 20,
                     "hypothesis": "The existing 20 day trend is the fixed control."},
                    {"name": "momentum_30d", "strategy": "momentum", "lookback_days": 30,
                     "hypothesis": "A slower trend may reduce turnover and short reversals."},
                    {"name": "reversion_20d", "strategy": "relative_reversion", "lookback_days": 20,
                     "hypothesis": "Past price declines may reverse when trend signals fail."},
                    {"name": "equal_weight", "strategy": "equal_weight", "lookback_days": 1,
                     "hypothesis": "Simple diversification may avoid signal switching errors."},
                    {"name": "cash", "strategy": "cash", "lookback_days": 1,
                     "hypothesis": "Cash avoids market losses and trading costs."}]}
    research = raw.get("daily_research", {})
    if not isinstance(research, dict) or set(research) - set(defaults):
        raise ValueError("unknown daily_research field")
    cfg["daily_research"] = policy = copy.deepcopy({**defaults, **research})
    for key in ("train_days", "validation_days", "activation_delay_days"):
        if isinstance(policy[key], bool) or not isinstance(policy[key], int) or policy[key] < (2 if key != "activation_delay_days" else 1):
            raise ValueError(f"daily research {key} has an invalid duration")
    for key in ("uncertainty_penalty", "min_validation_excess", "max_drawdown", "strategy_budget"):
        policy[key] = float(policy[key])
        if not math.isfinite(policy[key]) or policy[key] < 0:
            raise ValueError(f"daily research {key} must be finite and nonnegative")
    if not 0 < policy["max_drawdown"] <= 1 or not 0 < policy["strategy_budget"] <= 1:
        raise ValueError("drawdown and strategy budget must be in (0, 1]")
    specs = policy["candidates"]
    if not isinstance(specs, list) or not specs:
        raise ValueError("declare a nonempty candidate catalog")
    names = []
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != {"name", "strategy", "lookback_days", "hypothesis"}:
            raise ValueError("each candidate needs name, strategy, lookback_days and hypothesis")
        if (not isinstance(spec["name"], str) or not spec["name"] or spec["strategy"] not in STRATEGIES
                or not isinstance(spec["hypothesis"], str) or not spec["hypothesis"].strip()
                or isinstance(spec["lookback_days"], bool) or not isinstance(spec["lookback_days"], int)
                or spec["lookback_days"] < 1):
            raise ValueError("invalid daily candidate")
        names.append(spec["name"])
    if len(names) != len(set(names)) or "daily_mix" in names:
        raise ValueError("candidate names must be unique and cannot be daily_mix")
    if policy["initial_variant"] not in names or not any(s["name"] == "cash" and s["strategy"] == "cash" for s in specs):
        raise ValueError("initial variant and a cash fallback must exist")
    return cfg


def sample_stats(values: list[float], penalty: float) -> dict:
    if not values:
        raise ValueError("completed observations required")
    mean = math.fsum(values) / len(values)
    variance = math.fsum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in values:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {"samples": len(values), "net_return": equity - 1, "max_drawdown": drawdown,
            "mean_daily_return": mean, "score": mean - penalty * math.sqrt(variance / len(values))}


def propose_correction(history, observations: list[dict], active: str, policy: dict,
                       switch_costs: dict[str, float]) -> dict:
    """Select on training only, then accept/reject that single choice on validation."""
    if any(utc_timestamp(row["as_of"]) > history.as_of for row in observations):
        raise ValueError("future observations cannot enter daily research")
    total = policy["train_days"] + policy["validation_days"]
    if len(observations) < total:
        return {"action": "hold", "reason": "insufficient_completed_days", "observed_days": len(observations),
                "required_days": total}
    window = observations[-total:]
    train, validation = window[:policy["train_days"]], window[policy["train_days"]:]
    names = [spec["name"] for spec in policy["candidates"]]
    scores = {name: sample_stats([r["returns"][name] for r in train], policy["uncertainty_penalty"])
              for name in names}
    # Stable catalog order resolves ties; validation never ranks the catalog.
    eligible = [name for name in names if scores[name]["max_drawdown"] >= -policy["max_drawdown"]]
    chosen = max(eligible, key=lambda name: scores[name]["score"])
    candidate = sample_stats([r["returns"][chosen] for r in validation], policy["uncertainty_penalty"])
    incumbent = sample_stats([r["returns"][active] for r in validation], policy["uncertainty_penalty"])
    excess = sample_stats([r["returns"][chosen] - r["returns"][active] for r in validation],
                          policy["uncertainty_penalty"])
    estimated_cost = switch_costs[chosen]
    passed = (candidate["net_return"] >= 0 and candidate["max_drawdown"] >= -policy["max_drawdown"]
              and candidate["net_return"] - incumbent["net_return"] > policy["min_validation_excess"] + estimated_cost
              and excess["score"] > estimated_cost / len(validation))
    reason = "same_variant" if chosen == active else "validation_passed" if passed else "validation_rejected"
    return {"action": "revise" if chosen != active and passed else "hold", "reason": reason,
            "proposed_variant": chosen, "hypothesis": next(s["hypothesis"] for s in policy["candidates"] if s["name"] == chosen),
            "train_start": train[0]["as_of"], "train_end": train[-1]["as_of"],
            "validation_start": validation[0]["as_of"], "validation_end": validation[-1]["as_of"],
            "training_scores": scores, "candidate_validation": candidate, "incumbent_validation": incumbent,
            "paired_excess": excess, "estimated_switch_cost_fraction": estimated_cost,
            "cost_estimate_basis": "current holdings and current visible candidate target; future realized cost may differ"}


def simulate_daily(panel, raw: dict):
    cfg = resolve_config(raw)
    policy = cfg["daily_research"]
    replay = DailyReplay(panel.dates, panel.prices, cfg["data_delay_seconds"])
    specs = {s["name"]: s for s in policy["candidates"]}
    warmup = max(s["lookback_days"] for s in specs.values())
    active = policy["initial_variant"]
    version = digest({"rule": RULE, "initial": active})[:24]
    pending = None
    shadows = {name: _PortfolioLedger(name, "benchmark" if name in {active, "cash"} else "shadow", cfg, panel.assets)
               for name in specs}
    actual = _PortfolioLedger("daily_mix", "portfolio", cfg, panel.assets)
    accounts = [*shadows.values(), actual]
    observations, reviews, versions = [], [], []
    for index, on in enumerate(panel.dates):
        clock = replay.available_at(on)
        if index < warmup or (cfg["start_at"] and clock < utc_timestamp(cfg["start_at"])):
            continue
        if cfg["end_at"] and clock > utc_timestamp(cfg["end_at"]):
            break
        history = replay.advance_to(clock)
        if pending is not None and clock >= utc_timestamp(pending["effective_at"]):
            active, version = pending["to_variant"], pending["version_id"]
            actual.research_events.append({"event_type": "daily_revision_activated", "event_at": clock.isoformat(),
                                           "payload": dict(pending)})
            pending = None
        prior = {a.name: a.portfolio[-1]["equity"] if a.portfolio else cfg["initial_capital"] for a in accounts}
        old_trade_count = len(actual.trades)
        before = {a.name: a.observe(history) for a in accounts}
        if len(actual.portfolio) > 1:
            observations.append({"as_of": clock.isoformat(),
                                 "returns": {name: a.portfolio[-1]["equity"] / prior[name] - 1 for name, a in shadows.items()}})
        targets = {}
        for name, spec in specs.items():
            parameters = {**cfg, "lookback_days": spec["lookback_days"]}
            signal = STRATEGIES[spec["strategy"]](on, history, parameters)
            signal = _validate_targets(signal, history.assets, cfg["max_weight"], name)
            targets[name] = {asset: weight * policy["strategy_budget"] for asset, weight in signal.items()}
            shadows[name].submit(history, targets[name], {})
        values = {a: actual.units[a] * history.price(on, a) for a in history.assets}
        switch_costs = {}
        for name in specs:
            equity = actual.portfolio[-1]["equity"]
            notional = math.fsum(abs(targets[name].get(asset, 0) * equity - value) for asset, value in values.items())
            switch_costs[name] = notional / equity * cfg["transaction_cost_bps"] / 10000
        proposal = propose_correction(history, observations, active, policy, switch_costs)
        # A predeclared stop uses the actual rolling account, including switching costs.
        actual_window = actual.portfolio[-(policy["validation_days"] + 1):]
        actual_stats = sample_stats([b["equity"] / a["equity"] - 1 for a, b in zip(actual_window, actual_window[1:])]
                                   or [0.0], policy["uncertainty_penalty"])
        if actual_stats["max_drawdown"] < -policy["max_drawdown"] and active != "cash":
            proposal.update(action="revise", proposed_variant="cash", reason="actual_drawdown_stop",
                            hypothesis="Reduce exposure after the declared actual-account drawdown limit.")
        if pending is not None:
            proposal.update(action="hold", reason="revision_waiting_for_activation")
        if proposal["action"] == "revise":
            pending = {"from_variant": active, "to_variant": proposal["proposed_variant"],
                       "parent_version": version, "frozen_at": clock.isoformat(),
                       "effective_at": (clock + timedelta(days=policy["activation_delay_days"])).isoformat(),
                       "history_hash": history.content_hash, "reason": proposal["reason"],
                       "hypothesis": proposal["hypothesis"], "evidence_hash": digest(proposal)}
            pending["version_id"] = digest(pending)[:24]
            versions.append(dict(pending))
            actual.research_events.append({"event_type": "daily_revision_frozen", "event_at": clock.isoformat(),
                                           "payload": dict(pending)})
        today_fees = math.fsum(t["fee"] for t in actual.trades[old_trade_count:])
        today_notional = math.fsum(t["notional"] for t in actual.trades[old_trade_count:])
        row = actual.portfolio[-1]
        issues = []
        if row["equity"] < prior[actual.name]:
            issues.append("net_loss")
        if 0 <= before[actual.name] - prior[actual.name] < today_fees:
            issues.append("cost_exceeds_positive_market_pnl")
        if today_notional / row["equity"] > 0.5:
            issues.append("high_turnover")
        if row["cash"] / row["equity"] > 0.95:
            issues.append("mostly_cash")
        review = {"date": on, "as_of": clock.isoformat(), "history_hash": history.content_hash,
                  "active_variant": active, "active_version": version,
                  "equity": row["equity"], "daily_net_return": row["equity"] / prior[actual.name] - 1,
                  "market_pnl": before[actual.name] - prior[actual.name], "fees": today_fees,
                  "turnover": today_notional / before[actual.name], "cash_weight": row["cash"] / row["equity"],
                  "rolling_actual_drawdown": actual_stats["max_drawdown"], "issues": issues,
                  "reflection": proposal, "pending_revision": copy.deepcopy(pending),
                  "return_scope": "completed account mark; attribution follows previously submitted orders"}
        reviews.append(review)
        actual.research_events.append({"event_type": "daily_review", "event_at": clock.isoformat(), "payload": review})
        actual.allocations.append({"decision_date": on, "decision_at": clock.isoformat(), "rule": RULE,
                                   "selected_strategy": active, "daily_version": version, "reason": proposal["reason"],
                                   "asset_targets": targets[active], "history_hash": history.content_hash})
        actual.submit(history, targets[active], {})
    if not reviews:
        raise ValueError("no daily replay events after warmup")
    if cfg["start_at"] and utc_timestamp(reviews[0]["as_of"]) != utc_timestamp(cfg["start_at"]):
        raise ValueError("start must match an available daily event with sufficient warmup")
    state = {"accounts": accounts, "active_variant": active, "active_version": version,
             "pending_revision": pending, "completed_observations": observations[-(policy["train_days"] + policy["validation_days"]):],
             "as_of": reviews[-1]["as_of"], "history_hash": reviews[-1]["history_hash"]}
    return [a.finish() for a in accounts], reviews, versions, state


def audit_daily(panel, cfg, results, reviews, versions):
    accounts, errors = [], []
    for result in results:
        report, failures, _ = _account_report(result, panel, cfg)
        accounts.append(report)
        errors.extend(failures)
    frozen = {v["version_id"]: v for v in versions}
    actual = next(r for r in results if r.strategy == "daily_mix")
    rows = {r["date"]: r for r in actual.portfolio}
    allocations = {r["decision_date"]: r for r in actual.allocations}
    previous_equity = cfg["initial_capital"]
    replay = DailyReplay(panel.dates, panel.prices, cfg["data_delay_seconds"])
    for review in reviews:
        clock = utc_timestamp(review["as_of"])
        if replay.advance_to(clock).content_hash != review["history_hash"]:
            errors.append("daily history prefix mismatch")
        proposal = review["reflection"]
        row = rows[review["date"]]
        if (not math.isclose(review["daily_net_return"], row["equity"] / previous_equity - 1, abs_tol=1e-12)
                or not math.isclose(review["equity"], row["equity"], abs_tol=1e-8)):
            errors.append("daily reflection does not reconcile to completed account return")
        previous_equity = row["equity"]
        allocation = allocations[review["date"]]
        if (allocation["selected_strategy"] != review["active_variant"]
                or allocation["daily_version"] != review["active_version"]):
            errors.append("daily decision does not match reviewed active version")
        if "train_end" in proposal and not (utc_timestamp(proposal["train_end"]) < utc_timestamp(proposal["validation_start"])
                                            <= utc_timestamp(proposal["validation_end"]) <= clock):
            errors.append("daily training / validation chronology mismatch")
        if review["active_version"] in frozen:
            entry = frozen[review["active_version"]]
            if not utc_timestamp(entry["frozen_at"]) < utc_timestamp(entry["effective_at"]) <= clock:
                errors.append("daily revision used before activation")
    for entry in versions:
        if utc_timestamp(entry["effective_at"]) - utc_timestamp(entry["frozen_at"]) != timedelta(days=cfg["daily_research"]["activation_delay_days"]):
            errors.append("daily revision activation delay mismatch")
    return {"passed": not errors, "failure_count": len(errors), "examples": errors[:30]}, accounts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="daily_walkforward_config.json")
    parser.add_argument("--data")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--cost-bps", type=float)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        started = time.perf_counter()
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        for key, value in (("start_at", args.start), ("end_at", args.end), ("transaction_cost_bps", args.cost_bps)):
            if value is not None:
                raw[key] = value
        cfg = resolve_config(raw)
        if not cfg["start_at"] or not cfg["end_at"]:
            raise ValueError("declare start_at and end_at before running")
        if utc_timestamp(cfg["end_at"]) > now_utc():
            raise ValueError("historical replay cannot process future daily bars")
        panel = load_price_csv(args.data or raw["data"])
        root = Path(args.output)
        if root.exists() and any(root.iterdir()):
            raise ValueError("use a new or empty output directory; keep all previous attempts")
        engine = code_identity()
        source = Path(__file__).read_bytes().replace(b"\r\n", b"\n")
        plan = {"schema": RULE, "created_at": now_utc().isoformat(), "config": cfg,
                "engine": engine, "daily_code_sha256": hashlib.sha256(source).hexdigest(),
                "runtime": runtime_identity(), "data": price_identity(panel), "raw_data_hash": panel.content_hash,
                "nature": "retrospective daily adaptive research; reused history is not independent confirmation",
                "search_budget": "one declared catalog; one train-selected proposal per day; no validation reranking",
                "execution": "activate after declared delay, then submit for the next daily close",
                "data_vintage": "historical CSV snapshot; actual historical releases and revisions unknown"}
        write_immutable(root / "protocol.json", canonical_json(plan) + "\n")
        archive_code(root / "engine", engine)
        write_immutable(root / "engine" / "daily_walkforward.py", source.decode("utf-8"))
        results, reviews, versions, state = simulate_daily(panel, cfg)
        if utc_timestamp(reviews[-1]["as_of"]) != utc_timestamp(cfg["end_at"]):
            raise ValueError("end must match an available complete daily event; requested history is incomplete")
        audit, accounts = audit_daily(panel, cfg, results, reviews, versions)
        if not audit["passed"]:
            raise ValueError(f"daily independent audit failed: {audit}")
        run_id = digest(plan)
        db = setup_research_db(root / "daily.sqlite3")
        try:
            save_experiment(db, run_id, panel, cfg, results)
        finally:
            db.close()
        write_immutable(root / "daily-reviews.jsonl", "".join(canonical_json(r) + "\n" for r in reviews))
        write_immutable(root / "versions.json", canonical_json(versions) + "\n")
        write_immutable(root / "final-state.json", canonical_json(encode_state(state)) + "\n")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["account", "date", "as_of", "equity", "cash"])
        for result in results:
            for row in result.portfolio:
                writer.writerow([result.strategy, row["date"], row["as_of"], row["equity"], row["cash"]])
        write_immutable(root / "equity.csv", stream.getvalue())
        summary = {"run_id": run_id, "protocol_hash": digest(plan), "config": cfg, "audit": audit,
                   "start_at": reviews[0]["as_of"], "end_at": reviews[-1]["as_of"],
                   "equity_points": len(reviews), "completed_daily_intervals": len(reviews) - 1,
                   "reviews": len(reviews), "revisions_frozen": len(versions),
                   "revisions_activated": sum(e["event_type"] == "daily_revision_activated" for e in results[-1].research_events),
                   "reasons": dict(Counter(r["reflection"]["reason"] for r in reviews)),
                   "issues": dict(Counter(issue for r in reviews for issue in r["issues"])),
                   "accounts": accounts, "elapsed_seconds": time.perf_counter() - started,
                   "interpretation": plan["nature"]}
        write_immutable(root / "summary.json", canonical_json(summary) + "\n")
        lines = ["# 历史逐日复盘与修正", "", f"运行 `{run_id}`；独立账务与时序审计通过。", "",
                 f"{summary['start_at']} 至 {summary['end_at']}，{len(reviews) - 1} 个完整日区间，{len(reviews)} 次复盘，"
                 f"冻结 {len(versions)} 次修正，生效 {summary['revisions_activated']} 次。", "",
                 "| 账户 | 净收益 | 最大回撤 | 费用 |", "| --- | ---: | ---: | ---: |"]
        for account in accounts:
            m = account["metrics"]
            lines.append(f"| {account['account']} | {m['total_return']:.2%} | {m['max_drawdown']:.2%} | {m['total_fees']:.2f} |")
        lines += ["", "每日复盘见 daily-reviews.jsonl；修正与失败证据、连续账务见 daily.sqlite3；版本见 versions.json。",
                  "策略及窗口只在预先声明清单内调整；训练选择后只验证一个提案，验证不通过则保留现行版本。",
                  "每天复盘不等于每天改参数；训练和验证窗口为滚动重复使用，不能视为相互独立的试验。",
                  "本次属于事后历史研究，不构成独立盈利确认；未进行开放式代码生成。", ""]
        write_immutable(root / "report.md", "\n".join(lines))
        print(json.dumps({k: v for k, v in summary.items() if k != "accounts"}, ensure_ascii=False))
        for result in results:
            print(json.dumps({"account": result.strategy, **result.metrics}, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
