"""Independent ledger reconstruction and frozen-run JSON/Markdown/CSV reports."""

from __future__ import annotations

import csv
import io
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from historical_replay import utc_timestamp
from replay_artifacts import canonical_json, decode_state, price_identity, validate_checkpoint, write_immutable


def checkpoint_results(checkpoint: dict):
    validate_checkpoint(checkpoint)
    state = decode_state(checkpoint["state"])
    return [ledger.finish() for ledger in state["accounts"]]


def _version_key(result, allocation) -> str:
    if allocation is None:
        return "strategy:" + result.strategy
    selected = allocation.get("selected_strategy")
    for info in allocation.get("candidate_versions", {}).values():
        if selected == "candidate_" + info["version_id"]:
            return "candidate:" + info["version_id"]
    if "selector_version" in allocation:
        return "selector:" + (allocation["selector_version"] or "awaiting_feedback")
    return "rule:" + allocation["rule"]


def _account_report(result, panel, config) -> tuple[dict, list[str], dict]:
    failures = []
    initial = config["initial_capital"]
    cash = initial
    units = dict.fromkeys(panel.assets, 0.0)
    trades = defaultdict(list)
    for trade in result.trades:
        trades[trade["date"]].append(trade)
    allocations = {row["decision_date"]: row for row in result.allocations}
    executions = {row["execution_date"]: row for row in result.predictions}
    asset_pnl = dict.fromkeys(panel.assets, 0.0)
    asset_fees = dict.fromkeys(panel.assets, 0.0)
    version_pnl = defaultdict(lambda: {"market_pnl": 0.0, "fees": 0.0, "execution_days": 0})
    monthly = {}
    equity_lookup = {}
    previous_date = None
    previous_equity = initial
    holding_version = "initial_cash"
    peak, peak_date = initial, date.fromisoformat(result.portfolio[0]["date"])
    last_not_losing = peak_date
    underwater_days = loss_days = 0
    residual = 0.0
    observed_drawdown = turnover = 0.0
    rate = config["transaction_cost_bps"] / 10000.0
    for row in result.portfolio:
        on = row["date"]
        current_date = date.fromisoformat(on)
        if previous_date is not None:
            for asset in panel.assets:
                pnl = units[asset] * (panel.price(on, asset) - panel.price(previous_date, asset))
                asset_pnl[asset] += pnl
                version_pnl[holding_version]["market_pnl"] += pnl
        prediction = executions.get(on)
        if prediction is not None and not (utc_timestamp(prediction["decision_at"]) < utc_timestamp(prediction["execution_at"]) <= utc_timestamp(row["as_of"])):
            failures.append(f"{result.strategy}/{on}: execution chronology mismatch")
        before = cash + math.fsum(units[asset] * panel.price(on, asset) for asset in panel.assets)
        if len({trade["asset"] for trade in trades[on]}) != len(trades[on]):
            failures.append(f"{result.strategy}/{on}: duplicated net order")
        turnover += math.fsum(trade["notional"] for trade in trades[on]) / before
        executed_version = _version_key(result, allocations.get(prediction["decision_date"])) if prediction else holding_version
        for trade in trades[on]:
            if prediction is None:
                failures.append(f"{result.strategy}/{on}: trade has no submitted order")
            if trade["side"] not in {"buy", "sell"} or trade["notional"] < 0 or trade["fee"] < 0:
                failures.append(f"{result.strategy}/{on}: invalid trade side, notional or fee")
            if not math.isclose(trade["fee"], trade["notional"] * rate, rel_tol=1e-12, abs_tol=1e-12):
                failures.append(f"{result.strategy}/{on}: fee rate mismatch")
            if not math.isclose(trade["price"], panel.price(on, trade["asset"]), rel_tol=1e-12):
                failures.append(f"{result.strategy}/{on}: execution price mismatch")
            signed = trade["notional"] * (1 if trade["side"] == "buy" else -1)
            cash -= signed + trade["fee"]
            units[trade["asset"]] += signed / trade["price"]
            asset_fees[trade["asset"]] += trade["fee"]
            version_pnl[executed_version]["fees"] += trade["fee"]
        if prediction:
            holding_version = executed_version
            version_pnl[executed_version]["execution_days"] += 1
        values = {asset: units[asset] * panel.price(on, asset) for asset in panel.assets}
        equity = cash + math.fsum(values.values())
        residual = max(residual, abs(equity - row["equity"]), abs(cash - row["cash"]))
        if not math.isclose(equity, row["equity"], rel_tol=1e-10, abs_tol=1e-6):
            failures.append(f"{result.strategy}/{on}: equity does not reconcile")
        if not math.isclose(cash, row["cash"], rel_tol=1e-10, abs_tol=1e-6) or row["cash"] < 0:
            failures.append(f"{result.strategy}/{on}: cash does not reconcile")
        for asset in panel.assets:
            if not math.isclose(values[asset] / equity, row["weights"][asset], abs_tol=1e-10):
                failures.append(f"{result.strategy}/{on}: weights do not reconcile")
            if row["weights"][asset] > config["max_weight"] + 1e-12:
                failures.append(f"{result.strategy}/{on}: post-trade concentration limit exceeded")
        if not math.isclose(math.fsum(row["weights"].values()) + row["cash"] / row["equity"], 1, abs_tol=1e-12):
            failures.append(f"{result.strategy}/{on}: gross budget does not reconcile")
        if row["equity"] >= peak:
            peak, peak_date = row["equity"], current_date
        else:
            underwater_days = max(underwater_days, (current_date - peak_date).days)
        observed_drawdown = min(observed_drawdown, row["equity"] / peak - 1)
        if row["equity"] >= initial:
            last_not_losing = current_date
        else:
            loss_days = max(loss_days, (current_date - last_not_losing).days)
        month = on[:7]
        if month not in monthly:
            monthly[month] = {"month": month, "starting_equity": previous_equity,
                              "first_date": on, "last_date": on}
        monthly[month].update({"last_date": on, "ending_equity": row["equity"],
                               "net_return": row["equity"] / monthly[month]["starting_equity"] - 1})
        equity_lookup[on] = row["equity"]
        previous_date, previous_equity = on, row["equity"]
    net_change = result.portfolio[-1]["equity"] - initial
    checks = {"ending_equity": result.portfolio[-1]["equity"], "total_return": net_change / initial,
              "max_drawdown": observed_drawdown, "turnover": turnover,
              "total_fees": math.fsum(trade["fee"] for trade in result.trades),
              "rebalances": float(sum(bool(trades[row["date"]]) for row in result.portfolio))}
    for key, expected in checks.items():
        if not math.isclose(result.metrics[key], expected, rel_tol=1e-10, abs_tol=1e-10):
            failures.append(f"{result.strategy}: {key} metric does not reconcile")
    contribution = math.fsum(asset_pnl[a] - asset_fees[a] for a in panel.assets)
    version_change = math.fsum(row["market_pnl"] - row["fees"] for row in version_pnl.values())
    if not math.isclose(contribution, net_change, rel_tol=1e-10, abs_tol=1e-6):
        failures.append(f"{result.strategy}: asset contribution mismatch")
    if not math.isclose(version_change, net_change, rel_tol=1e-10, abs_tol=1e-6):
        failures.append(f"{result.strategy}: version contribution mismatch")
    for row in version_pnl.values():
        row["net_pnl"] = row["market_pnl"] - row["fees"]
    selected = [row.get("selected_strategy") for row in result.allocations]
    event_counts = Counter(e["event_type"] for e in (*result.selector_events, *result.research_events))
    report = {"account": result.strategy, "role": result.role, "start_at": result.portfolio[0]["as_of"],
              "end_at": result.portfolio[-1]["as_of"], "equity_points": len(result.portfolio), "metrics": result.metrics,
              "max_underwater_days": underwater_days, "max_days_below_initial_capital": loss_days,
              "selection_switches": sum(first != second for first, second in zip(selected, selected[1:])),
              "pending_orders": sum(e["event"] == "pending" for e in result.order_events),
              "minimum_cash": min(row["cash"] for row in result.portfolio), "max_ledger_residual": residual,
              "monthly": list(monthly.values()), "event_counts": dict(event_counts),
              "asset_contributions": {a: {"market_pnl": asset_pnl[a], "fees": asset_fees[a],
                                           "net_pnl": asset_pnl[a] - asset_fees[a]} for a in panel.assets},
              "version_contributions": dict(version_pnl)}
    fees = {on: math.fsum(trade["fee"] for trade in rows) for on, rows in trades.items()}
    return report, failures, {"equities": equity_lookup, "fees": fees}


def build_report(checkpoint: dict, panel, results=None) -> dict:
    validate_checkpoint(checkpoint)
    if price_identity(panel, checkpoint["known_data"]["last_date"]) != checkpoint["known_data"]:
        raise ValueError("report input does not match the checkpoint data")
    results = checkpoint_results(checkpoint) if results is None else results
    reports, failures, ledgers = [], [], {}
    for result in results:
        report, errors, lookup = _account_report(result, panel, checkpoint["manifest"]["protocol"]["config"])
        reports.append(report)
        failures.extend(errors)
        ledgers[result.strategy] = lookup
    created, matured, versions = {}, {}, {}
    candidates, confirmations, activations, retired = {}, {}, {}, {}
    shadow_starts = {}
    registrations = {}
    attempts = []
    for result in results:
        for event in (*result.selector_events, *result.research_events):
            kind, payload = event["event_type"], event["payload"]
            clock = utc_timestamp(event["event_at"])
            if clock > utc_timestamp(checkpoint["as_of"]):
                failures.append("event exceeds checkpoint clock")
            if kind == "feedback_created":
                identifier = payload["sample_id"]
                if identifier in created:
                    failures.append("duplicate feedback prediction")
                created[identifier] = payload
            elif kind == "feedback_matured":
                identifier = payload["sample_id"]
                label = created.get(identifier)
                if label is None or identifier in matured:
                    failures.append("mature label has no unique prediction")
                    continue
                if utc_timestamp(label["mature_at"]) > clock:
                    failures.append("feedback matured before its availability time")
                lookup = ledgers[label["strategy"]]
                expected = lookup["equities"][label["end_date"]] / (
                    lookup["equities"][label["execution_date"]] + lookup["fees"].get(label["execution_date"], 0)) - 1
                if not math.isclose(expected, payload["net_return"], rel_tol=1e-10, abs_tol=1e-12):
                    failures.append("mature label does not reconcile to its shadow account")
                matured[identifier] = event
            elif kind == "version_frozen":
                versions[payload["version_id"]] = payload
                if utc_timestamp(payload["effective_at"]) <= clock:
                    failures.append("selector version has no activation delay")
            elif kind == "version_activated":
                version = versions.get(payload["version_id"])
                if version is None or utc_timestamp(version["effective_at"]) > clock:
                    failures.append("selector version activated before becoming available")
            elif kind in {"research_attempt_completed", "research_attempt_failed"}:
                attempts.append({"at": event["event_at"], **payload})
                if payload["attempt_id"] not in registrations:
                    failures.append("research attempt has no registered hypothesis")
                if kind == "research_attempt_completed":
                    evidence = payload["evidence"]
                    if (utc_timestamp(evidence["train"]["end_at"]) >= utc_timestamp(evidence["validation"]["start_at"])
                            or utc_timestamp(evidence["validation"]["end_at"]) > clock):
                        failures.append("candidate internal research crossed its data boundary")
            elif kind == "hypothesis_registered":
                registrations[payload["attempt_id"]] = event
                if payload["attempt"] > payload["max_attempts"]:
                    failures.append("research attempt budget exceeded")
            elif kind == "candidate_frozen":
                candidates[payload["version_id"]] = payload
                if utc_timestamp(payload["shadow_at"]) <= clock:
                    failures.append("candidate shadow began without a freezing delay")
            elif kind == "candidate_shadow_started":
                identifier = payload["version_id"]
                version = candidates.get(identifier)
                if version is None or utc_timestamp(version["shadow_at"]) > clock:
                    failures.append("candidate shadow started before freeze availability")
                shadow_starts[identifier] = clock
                account = next((r for r in results if r.strategy == payload["account"]), None)
                if account is None or utc_timestamp(account.portfolio[0]["as_of"]) != clock:
                    failures.append("candidate shadow history was backfilled")
            elif kind == "candidate_confirmed":
                identifier = payload["version_id"]
                if identifier not in shadow_starts or utc_timestamp(payload["effective_at"]) <= clock:
                    failures.append("candidate confirmation has invalid timing")
                confirmations[identifier] = event
            elif kind == "candidate_activated":
                identifier = payload["version_id"]
                confirmation = confirmations.get(identifier)
                if confirmation is None or utc_timestamp(confirmation["payload"]["effective_at"]) > clock:
                    failures.append("candidate activated before confirmation became effective")
                activations[identifier] = clock
            elif kind in {"candidate_retired", "candidate_superseded"}:
                retired[payload["version_id"]] = clock
    # Validate evidence after collecting all labels. Availability, rather than
    # presence in this final report, determines whether a sample was usable.
    for version in versions.values():
        evidence = version["evidence"]
        split = utc_timestamp(evidence["validation_start"])
        frozen_at = utc_timestamp(version["frozen_at"])
        for identifier in evidence["training_ids"]:
            label = created.get(identifier)
            if (label is None or identifier not in matured or utc_timestamp(matured[identifier]["event_at"]) > split
                    or utc_timestamp(label["decision_at"]) >= split):
                failures.append("training label crosses the validation boundary")
        for identifier in evidence["validation_ids"]:
            label = created.get(identifier)
            if (label is None or identifier not in matured or utc_timestamp(label["decision_at"]) < split
                    or utc_timestamp(matured[identifier]["event_at"]) > frozen_at):
                failures.append("validation label was unavailable at freeze time")
    for result in results:
        for allocation in result.allocations:
            identifier = allocation.get("selector_version")
            if identifier and (identifier not in versions or utc_timestamp(versions[identifier]["effective_at"]) > utc_timestamp(allocation["decision_at"])):
                failures.append("decision used an unavailable selector version")
            for info in allocation.get("candidate_versions", {}).values():
                clock = utc_timestamp(allocation["decision_at"])
                identifier = info["version_id"]
                if (utc_timestamp(info["effective_at"]) > clock or identifier not in activations
                        or activations[identifier] > clock or (identifier in retired and retired[identifier] <= clock)):
                    failures.append("decision used an unavailable candidate version")
    for identifier, confirmation in confirmations.items():
        evidence = confirmation["payload"]["evidence"]
        for sample_id in evidence["candidate_ids"] + evidence["parent_ids"]:
            label = created.get(sample_id)
            if (label is None or sample_id not in matured or utc_timestamp(label["decision_at"]) < shadow_starts[identifier]
                    or utc_timestamp(matured[sample_id]["event_at"]) > utc_timestamp(confirmation["event_at"])):
                failures.append("candidate confirmation used unavailable or pre-freeze feedback")
    manifest = checkpoint["manifest"]
    origin_last = manifest["origin_data"]["last_date"]
    frozen_at = utc_timestamp(manifest["created_at"])
    base_rows = results[0].portfolio
    appended = [row for row in base_rows if row["date"] > origin_last]
    eligible = [row for row in appended if utc_timestamp(row["as_of"]) > frozen_at]
    eligible_dates = {row["date"] for row in eligible}
    continuation_accounts = []
    for result in results:
        selected_rows = [row for row in result.portfolio if row["date"] in eligible_dates]
        if not selected_rows:
            continue
        first_index = next(i for i, row in enumerate(result.portfolio) if row["date"] == selected_rows[0]["date"])
        anchor = result.portfolio[max(0, first_index - 1)]["equity"]
        continuation_accounts.append({"account": result.strategy, "starting_equity": anchor,
            "ending_equity": selected_rows[-1]["equity"], "net_return": selected_rows[-1]["equity"] / anchor - 1,
            "equity_points": len(selected_rows), "first_at": selected_rows[0]["as_of"], "last_at": selected_rows[-1]["as_of"]})
    return {"schema": "quantmp-frozen-report-v1", "run_id": manifest["run_id"],
            "checkpoint_hash": checkpoint["checkpoint_hash"], "parent_checkpoint": checkpoint["parent_hash"],
            "as_of": checkpoint["as_of"], "code_hash": manifest["code"]["hash"],
            "protocol": manifest["protocol"], "known_data": checkpoint["known_data"],
            "audit": {"passed": not failures, "failure_count": len(failures), "examples": failures[:30],
                      "feedback_created": len(created), "feedback_matured": len(matured),
                      "pending_feedback": len(created) - len(matured), "selector_versions": len(versions)},
            "confirmation": {"status": "post_freeze_rows_available_under_protocol" if eligible else "awaiting_post_freeze_rows",
                "frozen_at": manifest["created_at"], "appended_rows_processed": len(appended),
                "eligible_post_freeze_rows": len(eligible), "first_eligible_at": eligible[0]["as_of"] if eligible else None,
                "accounts": continuation_accounts,
                "independent_profitability_confirmed": False,
                "limitation": "CSV availability is a protocol assumption; publication, collection and revision vintages are unknown. Historical rows do not become independent confirmation by resuming."},
            "accounts": reports, "research_attempts": attempts,
            "attribution_method": "Market PnL follows the version responsible for holdings entering each interval; execution fees follow the submitting version. This is accounting attribution, not counterfactual selection advantage."}


def write_report(directory: str | Path, report: dict, results) -> dict[str, str]:
    root = Path(directory)
    suffix = report["checkpoint_hash"][:16]
    paths = {"json": root / f"report-{suffix}.json", "markdown": root / f"report-{suffix}.md",
             "equity_csv": root / f"equity-{suffix}.csv"}
    lines = ["# quantmp 冻结回放审计", "", f"运行：`{report['run_id']}`", "",
             f"检查点：`{report['checkpoint_hash']}`", "", f"时钟：{report['as_of']}", "",
             f"代码摘要：`{report['code_hash']}`", "",
             f"独立账务审计：{'通过' if report['audit']['passed'] else '失败'}；错误数 {report['audit']['failure_count']}。", "",
             "| 账户 | 角色 | 期末权益 | 净收益 | 最大回撤 | 累计费用 | 平均现金 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for account in report["accounts"]:
        metrics = account["metrics"]
        lines.append(f"| {account['account']} | {account['role']} | {metrics['ending_equity']:.2f} | {metrics['total_return']:.2%} | {metrics['max_drawdown']:.2%} | {metrics['total_fees']:.2f} | {metrics['average_cash_weight']:.2%} |")
    lines += ["", "## 结果边界", "",
              f"新增数据确认状态：`{report['confirmation']['status']}`，按协议符合冻结后可用时间的新增权益点为 {report['confirmation']['eligible_post_freeze_rows']} 个。", "",
              "历史回放、检查点恢复和再次下载历史数据不构成新的独立盈利确认。当前 CSV 没有真实发布／采集时间或逐次修订存档。", "",
              f"成熟标签 {report['audit']['feedback_matured']} 条，待定 {report['audit']['pending_feedback']} 条；待定订单和标签随检查点保留。", "",
              "JSON 包含逐月收益、回撤／亏损持续时间、资产和版本账务贡献、切换次数及完整研究失败结果；CSV 包含连续权益和现金。版本贡献按实际持仓区间及提交订单的费用归属，不表示反事实改进优势。", ""]
    if report["research_attempts"]:
        lines += ["## 候选研究", "", "| 候选 | 时钟 | 结果 |", "| --- | --- | --- |"]
        for attempt in report["research_attempts"]:
            lines.append(f"| {attempt['spec']['name']} | {attempt['at']} | {attempt['reason']} |")
        lines.append("")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["account", "role", "date", "as_of", "equity", "cash", "weights_json"])
    for result in results:
        for row in result.portfolio:
            writer.writerow([result.strategy, result.role, row["date"], row["as_of"], row["equity"], row["cash"], canonical_json(row["weights"])])
    write_immutable(paths["json"], canonical_json(report) + "\n")
    write_immutable(paths["markdown"], "\n".join(lines))
    write_immutable(paths["equity_csv"], stream.getvalue())
    return {name: str(path.resolve()) for name, path in paths.items()}
