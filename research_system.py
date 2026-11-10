"""Deterministic, local-only research loop for quantmp phase 1."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable


VERSION = "research-system-0.2.0"


@dataclass(frozen=True)
class PricePanel:
    dates: tuple[str, ...]
    assets: tuple[str, ...]
    prices: dict[str, dict[str, float]]
    source: str
    content_hash: str

    def price(self, on: str, asset: str) -> float:
        return self.prices[on][asset]


@dataclass(frozen=True)
class BacktestResult:
    strategy: str
    predictions: tuple[dict, ...]
    trades: tuple[dict, ...]
    portfolio: tuple[dict, ...]
    metrics: dict[str, float]


TargetFunction = Callable[[str, PricePanel, dict], dict[str, float]]


def _finite_float(value: str, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return result


def load_price_csv(path: str | Path) -> PricePanel:
    """Load a rectangular date/asset/close CSV without filling missing data."""
    file_path = Path(path)
    raw = file_path.read_bytes()
    rows: dict[tuple[str, str], float] = {}
    dates: set[str] = set()
    assets: set[str] = set()
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "asset", "close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("price CSV must contain date, asset and close columns")
        for line_number, row in enumerate(reader, 2):
            on = (row.get("date") or "").strip()
            asset = (row.get("asset") or "").strip()
            if not on or not asset:
                raise ValueError(f"line {line_number}: date and asset are required")
            try:
                date.fromisoformat(on)
            except ValueError as exc:
                raise ValueError(f"line {line_number}: date must be YYYY-MM-DD") from exc
            key = (on, asset)
            if key in rows:
                raise ValueError(f"duplicate price row for {on}/{asset}")
            rows[key] = _finite_float(row.get("close"), f"line {line_number} close")
            dates.add(on)
            assets.add(asset)
    if not rows or not dates or not assets:
        raise ValueError("price CSV contains no data")
    ordered_dates = tuple(sorted(dates))
    ordered_assets = tuple(sorted(assets))
    expected = len(ordered_dates) * len(ordered_assets)
    if len(rows) != expected:
        missing = [f"{on}/{asset}" for on in ordered_dates for asset in ordered_assets if (on, asset) not in rows]
        raise ValueError("price CSV is not rectangular; missing " + ", ".join(missing[:5]))
    prices = {
        on: {asset: rows[(on, asset)] for asset in ordered_assets}
        for on in ordered_dates
    }
    return PricePanel(ordered_dates, ordered_assets, prices, str(file_path), hashlib.sha256(raw).hexdigest())


def _cap_weights(scores: dict[str, float], max_weight: float) -> dict[str, float]:
    if not scores:
        return {}
    if not 0 < max_weight <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    remaining = dict(scores)
    weights = {asset: 0.0 for asset in scores}
    budget = 1.0
    while remaining and budget > 1e-12:
        total = sum(remaining.values())
        if total <= 0:
            break
        capped = []
        for asset in sorted(remaining):
            proposed = budget * remaining[asset] / total
            if proposed >= max_weight:
                weights[asset] = max_weight
                budget -= max_weight
                capped.append(asset)
        if not capped:
            for asset in remaining:
                weights[asset] = budget * remaining[asset] / total
            budget = 0.0
            break
        for asset in capped:
            remaining.pop(asset)
    return {asset: weight for asset, weight in weights.items() if weight > 1e-12}


def momentum_targets(on: str, panel: PricePanel, config: dict) -> dict[str, float]:
    lookback = int(config["lookback_days"])
    index = panel.dates.index(on)
    if index < lookback:
        return {}
    scores = {}
    prior = panel.dates[index - lookback]
    for asset in panel.assets:
        change = panel.price(on, asset) / panel.price(prior, asset) - 1.0
        if change > 0:
            scores[asset] = change
    return _cap_weights(scores, float(config["max_weight"]))


def relative_reversion_targets(on: str, panel: PricePanel, config: dict) -> dict[str, float]:
    """Allocate toward assets with negative cross-sectional lookback returns."""
    lookback = int(config["lookback_days"])
    index = panel.dates.index(on)
    if index < lookback:
        return {}
    prior = panel.dates[index - lookback]
    scores = {}
    for asset in panel.assets:
        change = panel.price(on, asset) / panel.price(prior, asset) - 1.0
        if change < 0:
            scores[asset] = -change
    return _cap_weights(scores, float(config["max_weight"]))


def equal_weight_targets(on: str, panel: PricePanel, config: dict) -> dict[str, float]:
    weight = 1.0 / len(panel.assets)
    return _cap_weights({asset: 1.0 for asset in panel.assets}, float(config["max_weight"])) if weight else {}


def cash_targets(on: str, panel: PricePanel, config: dict) -> dict[str, float]:
    return {}


STRATEGIES: dict[str, TargetFunction] = {
    "momentum": momentum_targets,
    "relative_reversion": relative_reversion_targets,
    "equal_weight": equal_weight_targets,
    "cash": cash_targets,
}


def _validate_config(config: dict) -> dict:
    result = {
        "initial_capital": float(config.get("initial_capital", 100000)),
        "lookback_days": int(config.get("lookback_days", 5)),
        "max_weight": float(config.get("max_weight", 1.0)),
        "transaction_cost_bps": float(config.get("transaction_cost_bps", 10)),
        "strategy_version": str(config.get("strategy_version", VERSION)),
    }
    if not math.isfinite(result["initial_capital"]) or result["initial_capital"] <= 0:
        raise ValueError("initial_capital must be positive")
    if result["lookback_days"] < 1:
        raise ValueError("lookback_days must be at least 1")
    if not 0 < result["max_weight"] <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    if not math.isfinite(result["transaction_cost_bps"]) or not 0 <= result["transaction_cost_bps"] < 10000:
        raise ValueError("transaction_cost_bps must be in [0, 10000) for unlevered spot execution")
    return result


def _rebalance(current_values: dict[str, float], cash: float, targets: dict[str, float],
               cost_rate: float) -> tuple[dict[str, float], dict[str, float], float, float]:
    """Solve post-cost targets without borrowing, charging costs on net trades.

    E_after + rate * sum(abs(weight * E_after - current_value)) = E_before.
    For a cost rate below 1 and gross weights at most 1, the left side is
    strictly increasing and piecewise linear. Starting at pre-cost equity,
    solve each segment until trade directions stop changing. Normalized,
    positive terms avoid cancellation near a 100% fee and large cash values.
    """
    value_before = cash + math.fsum(current_values.values())
    weights = {asset: targets.get(asset, 0.0) for asset in current_values}
    cash_weight = max(0.0, 1.0 - math.fsum(weights.values()))
    equity = value_before
    buys = {asset for asset, value in current_values.items() if weights[asset] * equity >= value}
    for _ in range(len(current_values) + 2):
        factors = {asset: 1.0 + cost_rate if asset in buys else 1.0 - cost_rate
                   for asset in current_values}
        numerator = math.fsum([cash / value_before, *(
            factors[asset] * (value / value_before) for asset, value in current_values.items())])
        denominator = math.fsum([cash_weight, *(
            factors[asset] * weight for asset, weight in weights.items())])
        equity = value_before * (numerator / denominator)
        next_buys = {asset for asset, value in current_values.items() if weights[asset] * equity >= value}
        if next_buys == buys:
            break
        buys = next_buys
    else:
        raise ArithmeticError("post-cost trade directions did not converge")
    target_values = {asset: weight * equity for asset, weight in weights.items()}
    trade_values = {asset: target_values[asset] - value for asset, value in current_values.items()}
    fee = cost_rate * math.fsum(abs(value) for value in trade_values.values())
    if not math.isclose(equity + fee, value_before, rel_tol=1e-12):
        raise ArithmeticError("post-cost rebalance does not conserve funds")
    return target_values, trade_values, equity * cash_weight, fee


def _metrics(portfolio: list[dict], initial_capital: float, turnover: float) -> dict[str, float]:
    equities = [row["equity"] for row in portfolio]
    if not equities:
        return {"ending_equity": initial_capital, "total_return": 0.0, "annualized_return": 0.0,
                "volatility": 0.0, "max_drawdown": 0.0, "turnover": turnover, "rebalances": 0.0}
    peak = equities[0]
    max_drawdown = 0.0
    returns = []
    for previous, current in zip(equities, equities[1:]):
        if previous > 0:
            returns.append(current / previous - 1.0)
        peak = max(peak, current)
        max_drawdown = min(max_drawdown, current / peak - 1.0)
    days = max(1, (date.fromisoformat(portfolio[-1]["date"]) - date.fromisoformat(portfolio[0]["date"])).days)
    total_return = equities[-1] / initial_capital - 1.0
    annualized = (equities[-1] / initial_capital) ** (365.0 / days) - 1.0 if equities[-1] > 0 else -1.0
    volatility = (sum((item - sum(returns) / len(returns)) ** 2 for item in returns) / (len(returns) - 1)) ** 0.5 * math.sqrt(365) if len(returns) > 1 else 0.0
    return {"ending_equity": equities[-1], "total_return": total_return, "annualized_return": annualized,
            "volatility": volatility, "max_drawdown": max_drawdown, "turnover": turnover,
            "rebalances": float(sum(1 for row in portfolio if row["traded"]))}


def backtest(panel: PricePanel, config: dict, strategy: str) -> BacktestResult:
    cfg = _validate_config(config)
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    lookback = cfg["lookback_days"]
    if len(panel.dates) <= lookback + 1:
        raise ValueError("price history is too short for the configured lookback")
    if any((date.fromisoformat(current) - date.fromisoformat(previous)).days != 1
           for previous, current in zip(panel.dates, panel.dates[1:])):
        raise ValueError("backtest requires consecutive daily prices; missing dates cannot be filled")
    target_fn = STRATEGIES[strategy]
    cash = cfg["initial_capital"]
    units = {asset: 0.0 for asset in panel.assets}
    predictions: list[dict] = []
    trades: list[dict] = []
    portfolio: list[dict] = [{
        "date": panel.dates[lookback], "equity": cash, "cash": cash,
        "weights": {asset: 0.0 for asset in panel.assets}, "traded": False,
    }]
    turnover = 0.0
    for index in range(lookback, len(panel.dates) - 1):
        decision_date = panel.dates[index]
        execution_date = panel.dates[index + 1]
        execution_prices = {asset: panel.price(execution_date, asset) for asset in panel.assets}
        value_before = cash + math.fsum(units[asset] * execution_prices[asset] for asset in panel.assets)
        targets = target_fn(decision_date, panel, cfg)
        if any(asset not in units for asset in targets):
            raise ValueError(f"{strategy} produced an unknown asset")
        targets = {asset: float(weight) for asset, weight in targets.items()}
        if any(not math.isfinite(weight) or weight < 0 for weight in targets.values()):
            raise ValueError(f"{strategy} produced a non-finite or negative target")
        targets = {asset: min(weight, cfg["max_weight"]) for asset, weight in targets.items()}
        target_total = math.fsum(targets.values())
        if target_total > 1.0 + 1e-12:
            raise ValueError(f"{strategy} produced more than 100% gross exposure")
        if target_total > 1.0:
            targets = {asset: weight / target_total for asset, weight in targets.items()}
        current_values = {asset: units[asset] * execution_prices[asset] for asset in panel.assets}
        target_values, trade_values, cash, _ = _rebalance(
            current_values, cash, targets, cfg["transaction_cost_bps"] / 10000.0)
        traded_value = math.fsum(abs(value) for value in trade_values.values())
        turnover += traded_value / value_before if value_before else 0.0
        units = {asset: target_values[asset] / execution_prices[asset] for asset in panel.assets}
        for asset in panel.assets:
            if trade_values[asset] != 0.0:
                trades.append({"date": execution_date, "asset": asset, "side": "buy" if trade_values[asset] > 0 else "sell",
                               "price": execution_prices[asset], "notional": abs(trade_values[asset]),
                               "fee": abs(trade_values[asset]) * cfg["transaction_cost_bps"] / 10000.0})
        predictions.append({"decision_date": decision_date, "execution_date": execution_date,
                            "targets": targets, "signals": {asset: panel.price(decision_date, asset) /
                            panel.price(panel.dates[index - lookback], asset) - 1.0 for asset in panel.assets}})
        equity = cash + math.fsum(target_values.values())
        portfolio.append({"date": execution_date, "equity": equity, "cash": cash,
                          "weights": {asset: target_values[asset] / equity for asset in panel.assets},
                          "traded": traded_value > 0.0})
    metrics = _metrics(portfolio, cfg["initial_capital"], turnover)
    return BacktestResult(strategy, tuple(predictions), tuple(trades), tuple(portfolio), metrics)


def experiment_id(panel: PricePanel, config: dict, strategies: Iterable[str]) -> str:
    payload = {"version": VERSION, "data_hash": panel.content_hash, "config": config, "strategies": list(strategies)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def setup_research_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS experiments (
        experiment_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data_source TEXT NOT NULL,
        data_hash TEXT NOT NULL, config_json TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS predictions (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, decision_date TEXT NOT NULL,
        execution_date TEXT NOT NULL, targets_json TEXT NOT NULL, signals_json TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy, decision_date)
    );
    CREATE TABLE IF NOT EXISTS trades (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, date TEXT NOT NULL, asset TEXT NOT NULL,
        side TEXT NOT NULL, price REAL NOT NULL, notional REAL NOT NULL, fee REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS portfolio_daily (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, date TEXT NOT NULL,
        equity REAL NOT NULL, cash REAL NOT NULL, weights_json TEXT NOT NULL, traded INTEGER NOT NULL,
        PRIMARY KEY (experiment_id, strategy, date)
    );
    CREATE TABLE IF NOT EXISTS metrics (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
        PRIMARY KEY (experiment_id, strategy, metric)
    );
    """)
    db.commit()
    return db


def save_experiment(db: sqlite3.Connection, experiment: str, panel: PricePanel, config: dict,
                    results: Iterable[BacktestResult]) -> None:
    """Append atomically; identical reruns are no-ops and conflicts are rejected."""
    selected = list(results)
    if not selected or len({result.strategy for result in selected}) != len(selected):
        raise ValueError("experiment requires non-empty, unique strategy results")
    records: dict[str, list[tuple]] = {table: [] for table in (
        "predictions", "trades", "portfolio_daily", "metrics")}
    for result in selected:
        for prediction in result.predictions:
            records["predictions"].append((
                experiment, result.strategy, prediction["decision_date"], prediction["execution_date"],
                json.dumps(prediction["targets"], sort_keys=True), json.dumps(prediction["signals"], sort_keys=True)))
        for trade in result.trades:
            records["trades"].append((
                experiment, result.strategy, trade["date"], trade["asset"], trade["side"],
                trade["price"], trade["notional"], trade["fee"]))
        for row in result.portfolio:
            records["portfolio_daily"].append((
                experiment, result.strategy, row["date"], row["equity"], row["cash"],
                json.dumps(row["weights"], sort_keys=True), int(row["traded"])))
        for metric, value in result.metrics.items():
            records["metrics"].append((experiment, result.strategy, metric, value))
    metadata = (panel.source, panel.content_hash, json.dumps(config, sort_keys=True), VERSION, "completed")
    with db:
        existing = db.execute(
            "SELECT data_source, data_hash, config_json, version, status FROM experiments WHERE experiment_id=?",
            (experiment,)).fetchone()
        if existing is not None:
            # Source paths are provenance, not identity; retain the first path
            # when identical input bytes are moved to a different directory.
            if existing[1:] != metadata[1:] or any(
                sorted(db.execute(f"SELECT * FROM {table} WHERE experiment_id=?", (experiment,)).fetchall())
                != sorted(rows) for table, rows in records.items()
            ):
                raise ValueError(f"experiment {experiment} is immutable; conflicting results cannot overwrite history")
            return
        created_at = datetime.now().astimezone().isoformat()
        db.execute("INSERT INTO experiments VALUES (?,?,?,?,?,?,?)", (experiment, created_at, *metadata))
        for table, rows in records.items():
            if rows:
                placeholders = ",".join("?" for _ in rows[0])
                db.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def run_experiment(data_path: str | Path, config: dict, strategies: Iterable[str], database: str | Path) -> tuple[str, list[BacktestResult]]:
    panel = load_price_csv(data_path)
    selected = list(strategies)
    if not selected:
        raise ValueError("at least one strategy is required")
    if len(set(selected)) != len(selected):
        raise ValueError("strategies must be unique")
    results = [backtest(panel, config, strategy) for strategy in selected]
    identifier = experiment_id(panel, config, selected)
    db = setup_research_db(database)
    try:
        save_experiment(db, identifier, panel, config, results)
    finally:
        db.close()
    return identifier, results
