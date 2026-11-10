"""Mature shadow feedback and a deterministic, purged regime selector.

Training scores and validation scores are research proxies from individual
shadow labels. Actual dynamic-portfolio returns always come from its ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Iterable, Mapping

from historical_replay import HistoryView, UnavailableDataError, daily_close, utc_timestamp, validate_delay


SELECTOR_RULE = "purged-regime-mean-v1"
STATES = ("up", "down_or_flat")


def market_state(signals: Mapping[str, float]) -> str:
    return "up" if math.fsum(signals.values()) > 0 else "down_or_flat"


def _event(kind: str, clock: datetime, payload: dict) -> dict:
    return {"event_type": kind, "event_at": clock.isoformat(), "payload": payload}


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    candidates: tuple[str, ...] = ("momentum", "relative_reversion")
    horizon_days: int = 5
    train_days: int = 180
    validation_days: int = 60
    research_every_days: int = 30
    activation_delay_days: int = 1
    min_train_samples: int = 12
    min_state_samples: int = 6
    min_validation_samples: int = 6
    min_active_validation_samples: int = 3
    uncertainty_penalty: float = 1.0
    strategy_budget: float = 1.0

    @classmethod
    def parse(cls, raw: Mapping, available: Iterable[str]) -> SelectorConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("selector must be a configuration object")
        defaults = asdict(cls())
        if set(raw) - set(defaults):
            raise ValueError("unknown selector configuration field")
        values = {**defaults, **raw}
        if not isinstance(values["candidates"], (list, tuple)):
            raise ValueError("selector candidates must be a list")
        names = tuple(values["candidates"])
        available = tuple(available)
        if (not names or any(not isinstance(name, str) for name in names) or len(set(names)) != len(names)
                or any(name not in available or name == "cash" for name in names)):
            raise ValueError("selector candidates must be unique known strategies; cash is the fallback")
        values["candidates"] = names
        for key, value in values.items():
            if key in {"candidates", "uncertainty_penalty", "strategy_budget"}:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"selector {key} must be a positive integer")
        for key in ("uncertainty_penalty", "strategy_budget"):
            try:
                values[key] = float(values[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"selector {key} must be a number") from exc
            if not math.isfinite(values[key]) or values[key] < 0:
                raise ValueError(f"selector {key} must be finite and non-negative")
        if values["strategy_budget"] > 1:
            raise ValueError("selector strategy_budget must be at most 1")
        if min(values["train_days"], values["validation_days"]) <= values["horizon_days"]:
            raise ValueError("selector windows must be longer than the label horizon")
        if min(values["min_train_samples"], values["min_state_samples"], values["min_validation_samples"]) < 2:
            raise ValueError("selector training and validation require at least two samples")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class MatureFeedback:
    sample_id: str
    strategy: str
    decision_date: str
    decision_at: datetime
    execution_date: str
    end_date: str
    mature_at: datetime
    observed_at: datetime
    state: str
    net_return: float

    def __post_init__(self):
        for name in ("decision_at", "mature_at", "observed_at"):
            object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
        if not self.decision_at < daily_close(self.execution_date) < daily_close(self.end_date) <= self.mature_at <= self.observed_at:
            raise ValueError("feedback timing must respect decision, execution, end and availability")
        if self.state not in STATES or not math.isfinite(self.net_return) or self.net_return < -1:
            raise ValueError("feedback state and return must be valid")


@dataclass(frozen=True, slots=True)
class FeedbackView:
    as_of: datetime
    samples: tuple[MatureFeedback, ...]

    def __post_init__(self):
        object.__setattr__(self, "as_of", utc_timestamp(self.as_of))
        object.__setattr__(self, "samples", tuple(self.samples))
        if any(sample.observed_at > self.as_of for sample in self.samples):
            raise ValueError("immature feedback cannot enter a training view")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("feedback sample IDs must be unique")
        if len({(sample.strategy, sample.decision_date) for sample in self.samples}) != len(self.samples):
            raise ValueError("feedback cannot overwrite a strategy's decision label")


class FeedbackBook:
    """Simulator-owned pending labels; only completed samples leave via view()."""

    def __init__(self, horizon_days: int, data_delay_seconds: float, strategy_version: str):
        if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
            raise ValueError("feedback horizon must be a positive integer")
        self.horizon_days = horizon_days
        self._delay = timedelta(seconds=validate_delay(data_delay_seconds))
        self._strategy_version = strategy_version
        self._pending: dict[str, dict] = {}
        self._entry_equities: dict[str, float] = {}
        self._samples: list[MatureFeedback] = []
        self._registered: set[tuple[str, str]] = set()
        self._clock: datetime | None = None

    def register(self, history: HistoryView, names: Iterable[str], state: str,
                 strategy_versions: Mapping[str, str] | None = None) -> list[dict]:
        if history.as_of != self._clock or state not in STATES:
            raise ValueError("feedback registration requires the current simulator clock and known state")
        on = date.fromisoformat(history.dates[-1])
        execution = (on + timedelta(days=1)).isoformat()
        end = (on + timedelta(days=self.horizon_days + 1)).isoformat()
        events = []
        for name in names:
            payload = {"strategy": name, "strategy_version": (strategy_versions or {}).get(name, self._strategy_version),
                       "decision_date": on.isoformat(), "decision_at": history.as_of.isoformat(),
                       "execution_date": execution, "end_date": end,
                       "mature_at": (daily_close(end) + self._delay).isoformat(),
                       "state": state, "history_hash": history.content_hash,
                       "label_type": "continuous_shadow_horizon_net_return"}
            identifier = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
            key = (name, on.isoformat())
            if key in self._registered:
                raise ValueError("feedback prediction already registered")
            self._registered.add(key)
            payload["sample_id"] = identifier
            self._pending[identifier] = payload
            events.append(_event("feedback_created", history.as_of, dict(payload)))
        return events

    def advance(self, history: HistoryView, before: Mapping[str, float], after: Mapping[str, float]) -> list[dict]:
        if self._clock is not None and history.as_of <= self._clock:
            raise ValueError("feedback clock must advance strictly")
        self._clock = history.as_of
        on = history.dates[-1]
        events = []
        for identifier, pending in list(self._pending.items()):
            name = pending["strategy"]
            if pending["execution_date"] == on:
                if not math.isfinite(before[name]) or before[name] <= 0:
                    raise ValueError("feedback entry equity must be finite and positive")
                self._entry_equities[identifier] = before[name]
            if pending["execution_date"] < on and identifier not in self._entry_equities:
                raise ValueError("missing feedback execution event")
            if pending["end_date"] < on:
                raise ValueError("missing feedback maturity event")
            if pending["end_date"] != on:
                continue
            net_return = after[name] / self._entry_equities[identifier] - 1.0
            sample = MatureFeedback(identifier, name, pending["decision_date"], utc_timestamp(pending["decision_at"]),
                                    pending["execution_date"], pending["end_date"], utc_timestamp(pending["mature_at"]),
                                    history.as_of, pending["state"], net_return)
            self._samples.append(sample)
            events.append(_event("feedback_matured", history.as_of, {
                "sample_id": identifier, "strategy": name, "end_date": on,
                "mature_at": sample.mature_at.isoformat(), "net_return": net_return,
            }))
            del self._pending[identifier]
            del self._entry_equities[identifier]
        return events

    def view(self, as_of: datetime) -> FeedbackView:
        if self._clock is None or utc_timestamp(as_of) > self._clock:
            raise UnavailableDataError("feedback view cannot advance the simulator clock")
        return FeedbackView(as_of, tuple(self._samples))


def _score(values: list[float], penalty: float) -> dict:
    mean = math.fsum(values) / len(values)
    std = math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)) if len(values) > 1 else 0.0
    return {"count": len(values), "mean": mean, "std": std,
            "score": mean - penalty * std / math.sqrt(len(values))}


@dataclass(frozen=True, slots=True)
class SelectorVersion:
    version_id: str
    frozen_at: datetime
    effective_at: datetime
    policy: Mapping[str, str]
    evidence_json: str
    status: str

    def __post_init__(self):
        object.__setattr__(self, "frozen_at", utc_timestamp(self.frozen_at))
        object.__setattr__(self, "effective_at", utc_timestamp(self.effective_at))
        object.__setattr__(self, "policy", MappingProxyType(dict(self.policy)))
        if self.effective_at <= self.frozen_at:
            raise ValueError("selector versions must become effective after freezing")
        if set(self.policy) != set(STATES) or self.status not in {"validated", "fallback_cash"}:
            raise ValueError("invalid selector policy or status")

    def record(self) -> dict:
        return {"version_id": self.version_id, "created_at": self.frozen_at.isoformat(),
                "trained_at": self.frozen_at.isoformat(), "frozen_at": self.frozen_at.isoformat(),
                "effective_at": self.effective_at.isoformat(), "policy": dict(self.policy),
                "status": self.status, "evidence": json.loads(self.evidence_json), "rule": SELECTOR_RULE}


def fit_selector(history: HistoryView, feedback: FeedbackView, config: SelectorConfig) -> tuple[SelectorVersion | None, dict]:
    if feedback.as_of != history.as_of:
        raise ValueError("training feedback must use the current history clock")
    if any((date.fromisoformat(s.end_date) - date.fromisoformat(s.execution_date)).days != config.horizon_days
           for s in feedback.samples if s.strategy in config.candidates):
        raise ValueError("feedback horizon does not match the frozen selector configuration")
    validation_start = history.as_of - timedelta(days=config.validation_days)
    train_start = validation_start - timedelta(days=config.train_days)
    eligible = [sample for sample in feedback.samples if sample.strategy in config.candidates
                and date.fromisoformat(sample.decision_date).toordinal() % (config.horizon_days + 1) == 0]
    train = sorted((s for s in eligible if train_start <= s.decision_at < validation_start
                    and s.observed_at <= validation_start), key=lambda s: (s.decision_at, s.strategy))
    validation = sorted((s for s in eligible if s.decision_at >= validation_start),
                        key=lambda s: (s.decision_at, s.strategy))
    evidence = {"data_cutoff": history.as_of.isoformat(), "history_hash": history.content_hash,
                "train_start": train_start.isoformat(), "validation_start": validation_start.isoformat(),
                "training_ids": [s.sample_id for s in train], "validation_ids": [s.sample_id for s in validation],
                "config": asdict(config), "label_usage": "research proxy; not actual portfolio returns"}
    pools = {name: [s for s in train if s.strategy == name] for name in config.candidates}
    if any(len(samples) < config.min_train_samples for samples in pools.values()):
        return None, {**evidence, "reason": "insufficient_mature_training"}
    policy = {}
    estimates = {}
    for state in STATES:
        estimates[state] = {}
        for name, samples in pools.items():
            subset = [s for s in samples if s.state == state]
            use_state = len(subset) >= config.min_state_samples
            used = subset if use_state else samples
            estimates[state][name] = {**_score([s.net_return for s in used], config.uncertainty_penalty),
                                      "scope": "state" if use_state else "pooled",
                                      "sample_ids": [s.sample_id for s in used]}
        qualified = [name for name in config.candidates if estimates[state][name]["score"] > 0]
        policy[state] = min(qualified, key=lambda name: (-estimates[state][name]["score"], name)) if qualified else "cash"
    groups: dict[str, dict[str, MatureFeedback]] = {}
    for sample in validation:
        groups.setdefault(sample.decision_date, {})[sample.strategy] = sample
    values = []
    active = 0
    selected_ids = []
    for on in sorted(groups):
        group = groups[on]
        if set(group) != set(config.candidates):
            continue
        states = {sample.state for sample in group.values()}
        if len(states) != 1:
            raise ValueError("validation states must agree across strategies")
        selected = policy[next(iter(states))]
        if selected == "cash":
            values.append(0.0)
        else:
            values.append(group[selected].net_return)
            selected_ids.append(group[selected].sample_id)
            active += 1
    evidence.update({"estimates": estimates, "proposed_policy": dict(policy),
                     "active_validation_samples": active, "selected_validation_ids": selected_ids})
    if len(values) < config.min_validation_samples:
        return None, {**evidence, "reason": "insufficient_mature_validation"}
    validation_score = _score(values, config.uncertainty_penalty)
    accepted = active >= config.min_active_validation_samples and validation_score["score"] > 0
    reason = "validation_passed" if accepted else "validation_failed_cash_fallback"
    evidence.update({"validation_proxy": validation_score, "reason": reason})
    final_policy = policy if accepted else {state: "cash" for state in STATES}
    frozen = history.as_of
    effective = frozen + timedelta(days=config.activation_delay_days)
    status = "validated" if accepted else "fallback_cash"
    payload = {"frozen_at": frozen.isoformat(), "effective_at": effective.isoformat(),
               "policy": final_policy, "evidence": evidence, "status": status, "rule": SELECTOR_RULE}
    identifier = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    return SelectorVersion(identifier, frozen, effective, final_policy, json.dumps(evidence, sort_keys=True), status), evidence


class RegimeSelector:
    def __init__(self, config: SelectorConfig):
        self.config = config
        self._clock: datetime | None = None
        self._next_research: datetime | None = None
        self._versions: list[SelectorVersion] = []
        self._active: SelectorVersion | None = None

    def visible_versions(self, as_of: datetime) -> tuple[SelectorVersion, ...]:
        clock = utc_timestamp(as_of)
        if self._clock is None or clock > self._clock:
            raise UnavailableDataError("version catalog cannot advance the simulator clock")
        return tuple(version for version in self._versions if version.effective_at <= clock)

    def advance(self, history: HistoryView, feedback: FeedbackView) -> list[dict]:
        clock = history.as_of
        if self._clock is not None and clock <= self._clock:
            raise ValueError("selector clock must advance strictly")
        self._clock = clock
        events = []
        visible = self.visible_versions(clock)
        if visible and (self._active is None or visible[-1].version_id != self._active.version_id):
            self._active = visible[-1]
            events.append(_event("version_activated", clock, {"version_id": self._active.version_id,
                                                             "effective_at": self._active.effective_at.isoformat()}))
        if self._next_research is None or clock >= self._next_research:
            version, evidence = fit_selector(history, feedback, self.config)
            if evidence["reason"] != "validation_passed":
                events.append(_event("research_rejected", clock, evidence))
            if version is not None:
                self._versions.append(version)
                events.append(_event("version_frozen", clock, version.record()))
            self._next_research = clock + timedelta(days=self.config.research_every_days)
        return events

    def decide(self, history: HistoryView, state: str) -> tuple[dict[str, float], dict]:
        if history.as_of != self._clock or state not in STATES:
            raise ValueError("selector decisions must use the current clock and known state")
        version = self._active
        selected = version.policy[state] if version is not None else "cash"
        budgets = {selected: self.config.strategy_budget} if selected != "cash" else {}
        return budgets, {"selector_version": version.version_id if version else None,
                         "selector_effective_at": version.effective_at.isoformat() if version else None,
                         "state": state, "selected_strategy": selected, "rule": SELECTOR_RULE,
                         "reason": ("awaiting_mature_feedback" if version is None else
                                    "validation_failed_cash_fallback" if version.status == "fallback_cash" else
                                    "validated_state_policy"),
                         "evidence_cutoff": version.frozen_at.isoformat() if version else None}
