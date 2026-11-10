"""Deterministic, local-only research loop for quantmp phase 1."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from historical_replay import (
    PROTOCOL_VERSION, DailyReplay, HistoryView, ScheduledTargets, utc_timestamp, validate_delay,
)
from strategy_selector import FeedbackBook, RegimeSelector, SELECTOR_RULE, SelectorConfig, market_state


VERSION = "research-system-0.5.0"
MIX_RULE = "fixed-strategy-budgets-v1"
DEFAULT_STRATEGY_BUDGETS = MappingProxyType({"momentum": 0.5, "relative_reversion": 0.5})


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
    order_events: tuple[dict, ...] = ()
    role: str = "standalone"
    allocations: tuple[dict, ...] = ()
    selector_events: tuple[dict, ...] = ()


TargetFunction = Callable[[str, HistoryView, Mapping], dict[str, float]]


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


def momentum_targets(on: str, panel: HistoryView, config: Mapping) -> dict[str, float]:
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


def relative_reversion_targets(on: str, panel: HistoryView, config: Mapping) -> dict[str, float]:
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


def equal_weight_targets(on: str, panel: HistoryView, config: Mapping) -> dict[str, float]:
    weight = 1.0 / len(panel.assets)
    return _cap_weights({asset: 1.0 for asset in panel.assets}, float(config["max_weight"])) if weight else {}


def cash_targets(on: str, panel: HistoryView, config: Mapping) -> dict[str, float]:
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
        "data_delay_seconds": validate_delay(config.get("data_delay_seconds", 0)),
        "start_at": utc_timestamp(config["start_at"]) if config.get("start_at") is not None else None,
        "end_at": utc_timestamp(config["end_at"]) if config.get("end_at") is not None else None,
    }
    if not math.isfinite(result["initial_capital"]) or result["initial_capital"] <= 0:
        raise ValueError("initial_capital must be positive")
    if result["lookback_days"] < 1:
        raise ValueError("lookback_days must be at least 1")
    if not 0 < result["max_weight"] <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    if not math.isfinite(result["transaction_cost_bps"]) or not 0 <= result["transaction_cost_bps"] < 10000:
        raise ValueError("transaction_cost_bps must be in [0, 10000) for unlevered spot execution")
    if result["start_at"] is not None and result["end_at"] is not None and result["start_at"] > result["end_at"]:
        raise ValueError("start_at must not be later than end_at")
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


def _validate_targets(targets: Mapping, assets: Iterable[str], max_weight: float, name: str) -> dict[str, float]:
    if not isinstance(targets, Mapping):
        raise ValueError(f"{name} must produce an object mapping assets to weights")
    if not 0 < max_weight <= 1:
        raise ValueError("max_weight must be in (0, 1]")
    if any(asset not in assets for asset in targets):
        raise ValueError(f"{name} produced an unknown asset")
    try:
        targets = {asset: float(weight) for asset, weight in targets.items()}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} target weights must be numbers") from exc
    if any(not math.isfinite(weight) or weight < 0 for weight in targets.values()):
        raise ValueError(f"{name} produced a non-finite or negative target")
    targets = {asset: min(weight, max_weight) for asset, weight in targets.items()}
    total = math.fsum(targets.values())
    if total > 1.0 + 1e-12:
        raise ValueError(f"{name} produced more than 100% gross exposure")
    if total > 1.0:
        targets = {asset: weight / total for asset, weight in targets.items()}
    return targets


def _validate_budgets(budgets: Mapping, candidates: Iterable[str]) -> dict[str, float]:
    if not isinstance(budgets, Mapping):
        raise ValueError("strategy_budgets must be an object mapping strategies to weights")
    names = tuple(candidates)
    if any(name not in names for name in budgets):
        raise ValueError("strategy_budgets contains an unknown strategy")
    try:
        result = {name: float(budgets.get(name, 0.0)) for name in names}
    except (TypeError, ValueError) as exc:
        raise ValueError("strategy budgets must be numbers") from exc
    if any(not math.isfinite(weight) or weight < 0 or weight > 1 for weight in result.values()):
        raise ValueError("strategy budgets must be finite and in [0, 1]")
    total = math.fsum(result.values())
    if total > 1.0 + 1e-12:
        raise ValueError("strategy budgets exceed 100% of portfolio capital")
    if total > 1.0:
        result = {name: weight / total for name, weight in result.items()}
    return result


def merge_strategy_targets(component_targets: Mapping[str, Mapping[str, float]],
                           budgets: Mapping[str, float], assets: Iterable[str],
                           max_weight: float) -> dict[str, float]:
    """Convert frozen strategy capital budgets into a single asset target."""
    budgets = _validate_budgets(budgets, component_targets)
    assets = tuple(assets)
    components = {name: _validate_targets(targets, assets, max_weight, name)
                  for name, targets in component_targets.items()}
    merged = {asset: math.fsum(budgets[name] * targets.get(asset, 0.0)
                              for name, targets in components.items()) for asset in assets}
    return _validate_targets({asset: weight for asset, weight in merged.items() if weight > 0},
                             assets, max_weight, "fixed_mix")


@dataclass
class _PortfolioLedger:
    name: str
    role: str
    config: dict
    assets: tuple[str, ...]
    cash: float = field(init=False)
    units: dict[str, float] = field(init=False)
    predictions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    portfolio: list[dict] = field(default_factory=list)
    order_events: list[dict] = field(default_factory=list)
    allocations: list[dict] = field(default_factory=list)
    selector_events: list[dict] = field(default_factory=list)
    pending: ScheduledTargets | None = None
    turnover: float = 0.0

    def __post_init__(self):
        self.cash = self.config["initial_capital"]
        self.units = dict.fromkeys(self.assets, 0.0)

    def observe(self, history: HistoryView) -> float:
        on = history.dates[-1]
        clock = history.as_of
        execution_prices = {asset: history.price(on, asset) for asset in self.assets}
        value_before = self.cash + math.fsum(self.units[asset] * execution_prices[asset] for asset in self.assets)
        traded_value = 0.0
        if self.pending is not None:
            if self.pending.execution_date != on:
                raise ValueError("missing execution bar for a pending order")
            filled_event = self.pending.event("filled", clock)
            current_values = {asset: self.units[asset] * execution_prices[asset] for asset in self.assets}
            target_values, trade_values, self.cash, _ = _rebalance(
                current_values, self.cash, dict(self.pending.targets), self.config["transaction_cost_bps"] / 10000.0)
            traded_value = math.fsum(abs(value) for value in trade_values.values())
            self.turnover += traded_value / value_before if value_before else 0.0
            self.units = {asset: target_values[asset] / execution_prices[asset] for asset in self.assets}
            for asset in self.assets:
                if trade_values[asset] != 0.0:
                    self.trades.append({"date": on, "asset": asset, "side": "buy" if trade_values[asset] > 0 else "sell",
                                        "price": execution_prices[asset], "notional": abs(trade_values[asset]),
                                        "fee": abs(trade_values[asset]) * self.config["transaction_cost_bps"] / 10000.0})
            self.order_events.append(filled_event)
            self.pending = None
        values = {asset: self.units[asset] * execution_prices[asset] for asset in self.assets}
        equity = self.cash + math.fsum(values.values())
        self.portfolio.append({"date": on, "as_of": clock.isoformat(), "equity": equity, "cash": self.cash,
                               "weights": {asset: values[asset] / equity for asset in self.assets},
                               "traded": traded_value > 0.0})
        return value_before

    def submit(self, history: HistoryView, targets: dict[str, float], signals: dict[str, float]) -> None:
        on = history.dates[-1]
        clock = history.as_of
        execution_date = (date.fromisoformat(on) + timedelta(days=1)).isoformat()
        self.pending = ScheduledTargets(on, clock, execution_date, targets)
        self.predictions.append({"decision_date": on, "decision_at": clock.isoformat(),
                                 "execution_date": execution_date, "execution_at": self.pending.execution_at.isoformat(),
                                 "history_hash": history.content_hash, "visible_rows": len(history.dates),
                                 "latest_available_at": history.timing(on).available_at.isoformat(),
                                 "protocol_version": PROTOCOL_VERSION, "targets": dict(self.pending.targets),
                                 "signals": dict(signals)})
        self.order_events.append(self.pending.event("submitted", clock))

    def finish(self) -> BacktestResult:
        events = list(self.order_events)
        if self.pending is not None:
            events.append(self.pending.event("pending", utc_timestamp(self.portfolio[-1]["as_of"])))
        metrics = _metrics(self.portfolio, self.config["initial_capital"], self.turnover)
        metrics["total_fees"] = math.fsum(trade["fee"] for trade in self.trades)
        metrics["average_cash_weight"] = math.fsum(row["cash"] / row["equity"] for row in self.portfolio) / len(self.portfolio)
        return BacktestResult(self.name, tuple(self.predictions), tuple(self.trades), tuple(self.portfolio),
                              metrics, tuple(events), self.role, tuple(self.allocations), tuple(self.selector_events))


def _allocate(ledger: _PortfolioLedger, history: HistoryView, components: dict[str, dict[str, float]],
              budgets: Mapping[str, float], signals: dict[str, float], rule: str, reason: str,
              details: dict | None = None) -> None:
    budgets = _validate_budgets(budgets, components)
    targets = merge_strategy_targets(components, budgets, history.assets, ledger.config["max_weight"])
    gross = math.fsum(targets.values())
    ledger.allocations.append({
        "decision_date": history.dates[-1], "decision_at": history.as_of.isoformat(), "history_hash": history.content_hash,
        "strategy_budgets": dict(budgets),
        "unallocated_cash_budget": max(0.0, 1.0 - math.fsum(budgets.values())),
        "strategy_targets": {name: dict(weights) for name, weights in components.items()},
        "asset_contributions": {name: {asset: budgets[name] * weight for asset, weight in weights.items()}
                                for name, weights in components.items()},
        "asset_targets": dict(targets), "cash_target": max(0.0, 1.0 - gross), "rule": rule, "reason": reason,
        "risk_check": {"gross_weight": gross, "max_asset_weight": max(targets.values(), default=0.0),
                       "max_weight": ledger.config["max_weight"], "passed": True},
        **(details or {}),
    })
    ledger.submit(history, targets, signals)


def _simulate(panel: PricePanel, config: dict, candidates: Iterable[str],
              budgets: Mapping[str, float] | None = None,
              selector_config: SelectorConfig | None = None) -> list[BacktestResult]:
    cfg = _validate_config(config)
    names = tuple(candidates)
    if not names or len(set(names)) != len(names) or any(name not in STRATEGIES for name in names):
        raise ValueError("replay requires non-empty, unique, known strategies")
    if budgets is not None and any(name in {"fixed_mix", "dynamic_mix"} for name in names):
        raise ValueError("fixed_mix and dynamic_mix are reserved for portfolio accounts")
    if selector_config is not None and budgets is None:
        raise ValueError("dynamic selection requires a fixed-budget benchmark")
    frozen_budgets = _validate_budgets(budgets, names) if budgets is not None else None
    functions = {name: STRATEGIES[name] for name in names}
    lookback = cfg["lookback_days"]
    if len(panel.dates) < lookback + 1:
        raise ValueError("price history is too short for the configured lookback")
    if any((date.fromisoformat(current) - date.fromisoformat(previous)).days != 1
           for previous, current in zip(panel.dates, panel.dates[1:])):
        raise ValueError("backtest requires consecutive daily prices; missing dates cannot be filled")
    replay = DailyReplay(panel.dates, panel.prices, cfg["data_delay_seconds"])
    strategy_config = MappingProxyType({key: cfg[key] for key in (
        "initial_capital", "lookback_days", "max_weight", "transaction_cost_bps", "strategy_version")})
    ledgers = {name: _PortfolioLedger(name, "shadow" if budgets is not None else "standalone", cfg, panel.assets)
               for name in names}
    actual = _PortfolioLedger("fixed_mix", "portfolio", cfg, panel.assets) if budgets is not None else None
    dynamic = _PortfolioLedger("dynamic_mix", "portfolio", cfg, panel.assets) if selector_config is not None else None
    selector = RegimeSelector(selector_config) if selector_config is not None else None
    feedback = FeedbackBook(selector_config.horizon_days, cfg["data_delay_seconds"], cfg["strategy_version"]) if selector else None
    if dynamic is not None:
        actual.role = "benchmark"
    accounts = [*ledgers.values(), *([actual] if actual is not None else [])]
    if dynamic is not None:
        accounts.append(dynamic)
    for index, on in enumerate(panel.dates):
        clock = replay.available_at(on)
        if index < lookback or (cfg["start_at"] is not None and clock < cfg["start_at"]):
            continue
        if cfg["end_at"] is not None and clock > cfg["end_at"]:
            break
        history = replay.advance_to(clock)
        before = {ledger.name: ledger.observe(history) for ledger in accounts}
        signals = {asset: history.price(on, asset) / history.price(history.dates[-lookback - 1], asset) - 1.0
                   for asset in history.assets}
        state = market_state(signals)
        if selector is not None:
            after = {name: ledger.portfolio[-1]["equity"] for name, ledger in ledgers.items()}
            dynamic.selector_events.extend(feedback.advance(history, before, after))
            dynamic.selector_events.extend(selector.advance(history, feedback.view(clock)))
        component_targets = {}
        for name, target_fn in functions.items():
            targets = _validate_targets(target_fn(on, history, strategy_config), history.assets, cfg["max_weight"], name)
            component_targets[name] = targets
            ledgers[name].submit(history, targets, signals)
        if actual is not None:
            _allocate(actual, history, component_targets, frozen_budgets, signals, MIX_RULE,
                      "frozen strategy budgets; unused strategy exposure remains cash")
        if selector is not None:
            selected_budgets, details = selector.decide(history, state)
            _allocate(dynamic, history, component_targets, selected_budgets, signals, SELECTOR_RULE,
                      details["reason"], details)
            dynamic.selector_events.extend(feedback.register(history, names, state))
    if not accounts[0].portfolio:
        raise ValueError("no replay events in the requested interval after warmup")
    return [ledger.finish() for ledger in accounts]


def backtest(panel: PricePanel, config: dict, strategy: str) -> BacktestResult:
    return _simulate(panel, config, [strategy])[0]


def backtest_fixed_mix(panel: PricePanel, config: dict) -> list[BacktestResult]:
    """Run every library strategy as an independent shadow and one funded mix."""
    budgets = _validate_budgets(config.get("strategy_budgets", DEFAULT_STRATEGY_BUDGETS), STRATEGIES)
    return _simulate(panel, config, STRATEGIES, budgets)


def backtest_dynamic_mix(panel: PricePanel, config: dict) -> list[BacktestResult]:
    """Run mature-feedback selection alongside fixed and independent controls."""
    budgets = _validate_budgets(config.get("strategy_budgets", DEFAULT_STRATEGY_BUDGETS), STRATEGIES)
    selector = SelectorConfig.parse(config.get("selector", {}), STRATEGIES)
    return _simulate(panel, config, STRATEGIES, budgets, selector)


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
    CREATE TABLE IF NOT EXISTS decision_audit (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, decision_date TEXT NOT NULL,
        decision_at TEXT NOT NULL, history_hash TEXT NOT NULL, visible_rows INTEGER NOT NULL,
        latest_available_at TEXT NOT NULL, protocol_version TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy, decision_date)
    );
    CREATE TABLE IF NOT EXISTS order_events (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, decision_date TEXT NOT NULL,
        event TEXT NOT NULL, event_at TEXT NOT NULL, processed_at TEXT NOT NULL,
        execution_date TEXT NOT NULL, execution_at TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy, decision_date, event)
    );
    CREATE TABLE IF NOT EXISTS account_roles (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy)
    );
    CREATE TABLE IF NOT EXISTS allocation_decisions (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, decision_date TEXT NOT NULL,
        allocation_json TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy, decision_date)
    );
    CREATE TABLE IF NOT EXISTS selector_events (
        experiment_id TEXT NOT NULL, strategy TEXT NOT NULL, sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL, event_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY (experiment_id, strategy, sequence)
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
    if any(result.role not in {"standalone", "shadow", "portfolio", "benchmark"} for result in selected):
        raise ValueError("unknown account role")
    records: dict[str, list[tuple]] = {table: [] for table in (
        "predictions", "trades", "portfolio_daily", "metrics", "decision_audit", "order_events",
        "account_roles", "allocation_decisions", "selector_events")}
    for result in selected:
        records["account_roles"].append((experiment, result.strategy, result.role))
        for prediction in result.predictions:
            records["predictions"].append((
                experiment, result.strategy, prediction["decision_date"], prediction["execution_date"],
                json.dumps(prediction["targets"], sort_keys=True), json.dumps(prediction["signals"], sort_keys=True)))
            records["decision_audit"].append((
                experiment, result.strategy, prediction["decision_date"], prediction["decision_at"],
                prediction["history_hash"], prediction["visible_rows"], prediction["latest_available_at"],
                prediction["protocol_version"]))
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
        for event in result.order_events:
            records["order_events"].append((
                experiment, result.strategy, event["decision_date"], event["event"], event["event_at"],
                event["processed_at"], event["execution_date"], event["execution_at"]))
        for allocation in result.allocations:
            records["allocation_decisions"].append((
                experiment, result.strategy, allocation["decision_date"], json.dumps(allocation, sort_keys=True)))
        for sequence, event in enumerate(result.selector_events):
            records["selector_events"].append((
                experiment, result.strategy, sequence, event["event_type"], event["event_at"],
                json.dumps(event["payload"], sort_keys=True)))
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
    results = _simulate(panel, config, selected)
    identifier = experiment_id(panel, config, selected)
    db = setup_research_db(database)
    try:
        save_experiment(db, identifier, panel, config, results)
    finally:
        db.close()
    return identifier, results


def run_fixed_mix_experiment(data_path: str | Path, config: dict,
                             database: str | Path) -> tuple[str, list[BacktestResult]]:
    """Freeze budgets, persist one actual portfolio and all independent shadows."""
    panel = load_price_csv(data_path)
    budgets = _validate_budgets(config.get("strategy_budgets", DEFAULT_STRATEGY_BUDGETS), STRATEGIES)
    frozen_config = {**config, "strategy_budgets": budgets, "portfolio_rule": MIX_RULE,
                     "shadow_strategies": list(STRATEGIES)}
    results = backtest_fixed_mix(panel, frozen_config)
    identifier = experiment_id(panel, frozen_config, [result.strategy for result in results])
    db = setup_research_db(database)
    try:
        save_experiment(db, identifier, panel, frozen_config, results)
    finally:
        db.close()
    return identifier, results


def run_dynamic_mix_experiment(data_path: str | Path, config: dict,
                               database: str | Path) -> tuple[str, list[BacktestResult]]:
    panel = load_price_csv(data_path)
    budgets = _validate_budgets(config.get("strategy_budgets", DEFAULT_STRATEGY_BUDGETS), STRATEGIES)
    selector = SelectorConfig.parse(config.get("selector", {}), STRATEGIES)
    frozen_config = {**config, "strategy_budgets": budgets, "portfolio_rule": MIX_RULE,
                     "shadow_strategies": list(STRATEGIES), "selector": asdict(selector), "selector_rule": SELECTOR_RULE}
    results = backtest_dynamic_mix(panel, frozen_config)
    identifier = experiment_id(panel, frozen_config, [result.strategy for result in results])
    db = setup_research_db(database)
    try:
        save_experiment(db, identifier, panel, frozen_config, results)
    finally:
        db.close()
    return identifier, results
