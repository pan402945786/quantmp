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


VERSION = "research-system-0.1.0"


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
    if not math.isfinite(result["transaction_cost_bps"]) or result["transaction_cost_bps"] < 0:
        raise ValueError("transaction_cost_bps must be non-negative")
    return result


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
    target_fn = STRATEGIES[strategy]
    cash = cfg["initial_capital"]
    units = {asset: 0.0 for asset in panel.assets}
    previous_weights = {asset: 0.0 for asset in panel.assets}
    predictions: list[dict] = []
    trades: list[dict] = []
    portfolio: list[dict] = []
    turnover = 0.0
    for index in range(lookback, len(panel.dates) - 1):
        decision_date = panel.dates[index]
        execution_date = panel.dates[index + 1]
        execution_prices = {asset: panel.price(execution_date, asset) for asset in panel.assets}
        value_before = cash + sum(units[asset] * execution_prices[asset] for asset in panel.assets)
        targets = target_fn(decision_date, panel, cfg)
        targets = {asset: max(0.0, min(float(weight), cfg["max_weight"])) for asset, weight in targets.items()}
        target_total = sum(targets.values())
        if target_total > 1.0 + 1e-9:
            raise ValueError(f"{strategy} produced more than 100% gross exposure")
        current_values = {asset: units[asset] * execution_prices[asset] for asset in panel.assets}
        target_values = {asset: value_before * targets.get(asset, 0.0) for asset in panel.assets}
        trade_values = {asset: target_values[asset] - current_values[asset] for asset in panel.assets}
        traded_value = sum(abs(value) for value in trade_values.values())
        turnover += traded_value / value_before if value_before else 0.0
        fee = traded_value * cfg["transaction_cost_bps"] / 10000.0
        cash = value_before - sum(target_values.values()) - fee
        units = {asset: target_values[asset] / execution_prices[asset] for asset in panel.assets}
        for asset in panel.assets:
            if abs(trade_values[asset]) > 1e-9:
                trades.append({"date": execution_date, "asset": asset, "side": "buy" if trade_values[asset] > 0 else "sell",
                               "price": execution_prices[asset], "notional": abs(trade_values[asset]),
                               "fee": fee * abs(trade_values[asset]) / traded_value if traded_value else 0.0})
        predictions.append({"decision_date": decision_date, "execution_date": execution_date,
                            "targets": targets, "signals": {asset: panel.price(decision_date, asset) /
                            panel.price(panel.dates[index - lookback], asset) - 1.0 for asset in panel.assets}})
        equity = cash + sum(units[asset] * execution_prices[asset] for asset in panel.assets)
        portfolio.append({"date": execution_date, "equity": equity, "cash": cash,
                          "weights": {asset: target_values[asset] / value_before for asset in panel.assets},
                          "traded": traded_value > 1e-9})
        previous_weights = targets
    final_date = panel.dates[-1]
    final_equity = cash + sum(units[asset] * panel.price(final_date, asset) for asset in panel.assets)
    if portfolio and portfolio[-1]["date"] != final_date:
        portfolio.append({"date": final_date, "equity": final_equity, "cash": cash,
                          "weights": {asset: units[asset] * panel.price(final_date, asset) / final_equity
                                      for asset in panel.assets}, "traded": False})
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
    created_at = datetime.now().astimezone().isoformat()
    db.execute("INSERT OR REPLACE INTO experiments VALUES (?,?,?,?,?,?,?)", (
        experiment, created_at, panel.source, panel.content_hash,
        json.dumps(config, sort_keys=True), VERSION, "completed"))
    for result in results:
        db.execute("DELETE FROM predictions WHERE experiment_id=? AND strategy=?", (experiment, result.strategy))
        db.execute("DELETE FROM trades WHERE experiment_id=? AND strategy=?", (experiment, result.strategy))
        db.execute("DELETE FROM portfolio_daily WHERE experiment_id=? AND strategy=?", (experiment, result.strategy))
        db.execute("DELETE FROM metrics WHERE experiment_id=? AND strategy=?", (experiment, result.strategy))
        for prediction in result.predictions:
            db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?)", (
                experiment, result.strategy, prediction["decision_date"], prediction["execution_date"],
                json.dumps(prediction["targets"], sort_keys=True), json.dumps(prediction["signals"], sort_keys=True)))
        for trade in result.trades:
            db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)", (
                experiment, result.strategy, trade["date"], trade["asset"], trade["side"],
                trade["price"], trade["notional"], trade["fee"]))
        for row in result.portfolio:
            db.execute("INSERT INTO portfolio_daily VALUES (?,?,?,?,?,?,?)", (
                experiment, result.strategy, row["date"], row["equity"], row["cash"],
                json.dumps(row["weights"], sort_keys=True), int(row["traded"])))
        for metric, value in result.metrics.items():
            db.execute("INSERT INTO metrics VALUES (?,?,?,?)", (experiment, result.strategy, metric, value))
    db.commit()


def run_experiment(data_path: str | Path, config: dict, strategies: Iterable[str], database: str | Path) -> tuple[str, list[BacktestResult]]:
    panel = load_price_csv(data_path)
    selected = list(strategies)
    if not selected:
        raise ValueError("at least one strategy is required")
    results = [backtest(panel, config, strategy) for strategy in selected]
    identifier = experiment_id(panel, config, selected)
    db = setup_research_db(database)
    try:
        save_experiment(db, identifier, panel, config, results)
    finally:
        db.close()
    return identifier, results
