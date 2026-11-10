"""Bounded, auditable candidate research and prospective parent-slot replacement.

Candidate definitions are a predeclared engineering plan, not an unrestricted
code generator. Historical experiments never become live shadow records.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping

from historical_replay import HistoryView, UnavailableDataError, utc_timestamp
from strategy_selector import FeedbackView, MatureFeedback, _score


RESEARCH_RULE = "bounded-parent-slot-research-v1"
COMPARISON_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    name: str
    parent: str
    lookback_days: int
    hypothesis: str


DEFAULT_PLAN = (
    CandidateSpec("momentum_slow_v1", "momentum", 20,
                  "A longer trend window may reduce churn and sensitivity to short reversals."),
    CandidateSpec("reversion_slow_v1", "relative_reversion", 20,
                  "A longer reversal window may separate persistent declines from short noise."),
)


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    candidate_plan: tuple[CandidateSpec, ...] = DEFAULT_PLAN
    max_attempts: int = 2
    attempts_per_cycle: int = 1
    research_every_days: int = 30
    trigger_cash_days: int = 60
    min_mature_samples: int = 12
    train_days: int = 180
    validation_days: int = 60
    shadow_delay_days: int = 1
    confirmation_days: int = 90
    confirmation_timeout_days: int = 150
    min_confirmation_samples: int = 12
    activation_delay_days: int = 1
    min_return: float = 0.0
    min_parent_excess: float = 0.0
    max_drawdown: float = 0.35
    uncertainty_penalty: float = 1.0

    @classmethod
    def parse(cls, raw: Mapping, parents: Iterable[str]) -> ResearchConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("research must be a configuration object")
        defaults = asdict(cls())
        if set(raw) - set(defaults):
            raise ValueError("unknown research configuration field")
        values = {**defaults, **raw}
        plan = values["candidate_plan"]
        if not isinstance(plan, (tuple, list)):
            raise ValueError("candidate_plan must be a list")
        parents = tuple(parents)
        specs = []
        for item in plan:
            if not isinstance(item, Mapping) or set(item) != {"name", "parent", "lookback_days", "hypothesis"}:
                raise ValueError("candidate definitions require name, parent, lookback_days and hypothesis")
            spec = CandidateSpec(**item)
            if (not isinstance(spec.name, str) or not spec.name or not isinstance(spec.parent, str)
                    or spec.parent not in parents or spec.parent == "cash"
                    or not isinstance(spec.hypothesis, str) or not spec.hypothesis.strip()):
                raise ValueError("candidate name, known parent and economic hypothesis are required")
            if isinstance(spec.lookback_days, bool) or not isinstance(spec.lookback_days, int) or spec.lookback_days < 1:
                raise ValueError("candidate lookback_days must be a positive integer")
            specs.append(spec)
        if not specs or len({spec.name for spec in specs}) != len(specs):
            raise ValueError("candidate plan must be non-empty and names must be unique")
        values["candidate_plan"] = tuple(specs)
        for key, value in values.items():
            if key == "candidate_plan" or key in {"min_return", "min_parent_excess", "max_drawdown", "uncertainty_penalty"}:
                continue
            minimum = 0 if key == "max_attempts" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"research {key} must be an integer >= {minimum}")
        for key in ("min_return", "min_parent_excess", "max_drawdown", "uncertainty_penalty"):
            try:
                values[key] = float(values[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"research {key} must be a number") from exc
            if not math.isfinite(values[key]) or values[key] < 0:
                raise ValueError(f"research {key} must be finite and non-negative")
        if not 0 < values["max_drawdown"] <= 1:
            raise ValueError("research max_drawdown must be in (0, 1]")
        if values["confirmation_timeout_days"] < values["confirmation_days"]:
            raise ValueError("confirmation timeout must not precede its minimum duration")
        if min(values["train_days"], values["validation_days"]) < 2:
            raise ValueError("research train and validation windows require at least two days")
        if min(values["min_mature_samples"], values["min_confirmation_samples"]) < 2:
            raise ValueError("research requires at least two mature samples")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CandidateVersion:
    version_id: str
    spec: CandidateSpec
    frozen_at: datetime
    shadow_at: datetime
    parent_version: str
    evidence_json: str

    @property
    def account(self) -> str:
        return "candidate_" + self.version_id

    def record(self) -> dict:
        return {"version_id": self.version_id, "account": self.account, "spec": asdict(self.spec),
                "parent_version": self.parent_version, "created_at": self.frozen_at.isoformat(),
                "research_completed_at": self.frozen_at.isoformat(), "frozen_at": self.frozen_at.isoformat(),
                "shadow_at": self.shadow_at.isoformat(), "evidence": json.loads(self.evidence_json),
                "data_requirements": "released daily close prices only",
                "research_nature": "retrospective engineering candidate; prospective shadow required"}


@dataclass
class _CandidateState:
    version: CandidateVersion
    status: str = "frozen"
    shadow_started_at: datetime | None = None
    effective_at: datetime | None = None
    retire_at: datetime | None = None
    next_review: datetime | None = None
    peak: float = 0.0
    max_drawdown: float = 0.0


class CandidateResearch:
    """All state changes are forward events; only effective replacements escape."""

    def __init__(self, config: ResearchConfig, horizon_days: int, parent_version: str):
        self.config = config
        self.horizon_days = horizon_days
        self.parent_version = parent_version
        self._clock: datetime | None = None
        self._next_research: datetime | None = None
        self._cash_streak = 0
        self._attempts = 0
        self._states: dict[str, _CandidateState] = {}
        self._budget_reported = False
        self._active: dict[str, _CandidateState] = {}

    def _event(self, kind: str, payload: dict) -> dict:
        return {"event_type": kind, "event_at": self._clock.isoformat(), "payload": payload}

    def begin_tick(self, history: HistoryView) -> tuple[list[CandidateVersion], list[dict]]:
        clock = history.as_of
        if self._clock is not None and clock <= self._clock:
            raise ValueError("research clock must advance strictly")
        self._clock = clock
        new_shadows = []
        events = []
        for state in self._states.values():
            version = state.version
            if state.status == "frozen" and version.shadow_at <= clock:
                state.status = "shadow"
                state.shadow_started_at = clock
                new_shadows.append(version)
                events.append(self._event("candidate_shadow_started", {"version_id": version.version_id,
                                                                        "account": version.account}))
            if state.status == "confirmed" and state.effective_at <= clock:
                previous = self._active.get(version.spec.parent)
                if previous is not None:
                    previous.status = "superseded"
                    events.append(self._event("candidate_superseded", {
                        "version_id": previous.version.version_id, "replacement": version.version_id}))
                state.status = "active"
                self._active[version.spec.parent] = state
                state.next_review = clock + timedelta(days=self.config.research_every_days)
                events.append(self._event("candidate_activated", {"version_id": version.version_id,
                            "parent": version.spec.parent, "effective_at": state.effective_at.isoformat()}))
            if state.status == "retiring" and state.retire_at <= clock:
                state.status = "retired"
                if self._active.get(version.spec.parent) is state:
                    del self._active[version.spec.parent]
                events.append(self._event("candidate_retired", {"version_id": version.version_id,
                                                               "retire_at": state.retire_at.isoformat()}))
        return new_shadows, events

    def replacements(self, as_of: datetime) -> dict[str, CandidateVersion]:
        clock = utc_timestamp(as_of)
        if clock != self._clock:
            raise UnavailableDataError("candidate catalog requires the current simulator clock")
        return {parent: state.version for parent, state in self._active.items()}

    def replacement_evidence(self, as_of: datetime) -> dict[str, dict]:
        self.replacements(as_of)
        return {parent: {"version_id": state.version.version_id,
                         "effective_at": state.effective_at.isoformat(),
                         "frozen_at": state.version.frozen_at.isoformat()}
                for parent, state in self._active.items()}

    def _shadow_evidence(self, state: _CandidateState, feedback: FeedbackView) -> dict:
        version = state.version
        first = max(state.shadow_started_at, self._clock - timedelta(days=self.config.confirmation_days))
        samples = [s for s in feedback.samples if s.strategy in {version.account, version.spec.parent}
                   and s.decision_at >= first
                   and datetime.fromisoformat(s.decision_date).date().toordinal() % (self.horizon_days + 1) == 0]
        groups: dict[str, dict[str, MatureFeedback]] = {}
        for sample in samples:
            groups.setdefault(sample.decision_date, {})[sample.strategy] = sample
        pairs = [group for _, group in sorted(groups.items()) if set(group) == {version.account, version.spec.parent}]
        returns = [group[version.account].net_return for group in pairs]
        excess = [group[version.account].net_return - group[version.spec.parent].net_return for group in pairs]
        return {"sample_count": len(pairs), "candidate_ids": [group[version.account].sample_id for group in pairs],
                "parent_ids": [group[version.spec.parent].sample_id for group in pairs],
                "return_score": _score(returns, self.config.uncertainty_penalty) if returns else None,
                "excess_score": _score(excess, self.config.uncertainty_penalty) if excess else None,
                "max_drawdown": state.max_drawdown, "data_cutoff": self._clock.isoformat(),
                "shadow_started_at": state.shadow_started_at.isoformat(),
                "label_usage": "prospective paired shadow proxy, not adaptive portfolio returns"}

    def finish_tick(self, history: HistoryView, feedback: FeedbackView, selected_parent: str,
                    shadow_equities: Mapping[str, float],
                    evaluate: Callable[[HistoryView, CandidateSpec], dict]) -> list[dict]:
        if history.as_of != self._clock or feedback.as_of != self._clock:
            raise ValueError("research requires current history and mature feedback")
        events = []
        for state in self._states.values():
            version = state.version
            if state.shadow_started_at is None:
                continue
            equity = shadow_equities[version.account]
            if not math.isfinite(equity) or equity <= 0:
                raise ValueError("candidate shadow equity must be finite and positive")
            state.peak = max(state.peak, equity)
            state.max_drawdown = min(state.max_drawdown, equity / state.peak - 1)
            age = (self._clock - state.shadow_started_at).days
            if state.status == "shadow" and age >= self.config.confirmation_days:
                evidence = self._shadow_evidence(state, feedback)
                enough = evidence["sample_count"] >= self.config.min_confirmation_samples
                accepted = (enough and evidence["return_score"]["score"] > self.config.min_return
                            and evidence["excess_score"]["score"] >= self.config.min_parent_excess - COMPARISON_TOLERANCE
                            and state.max_drawdown >= -self.config.max_drawdown - COMPARISON_TOLERANCE)
                if accepted:
                    state.status = "confirmed"
                    state.effective_at = self._clock + timedelta(days=self.config.activation_delay_days)
                    events.append(self._event("candidate_confirmed", {"version_id": version.version_id,
                        "effective_at": state.effective_at.isoformat(), "evidence": evidence}))
                elif enough or age >= self.config.confirmation_timeout_days:
                    state.status = "rejected"
                    events.append(self._event("candidate_rejected", {"version_id": version.version_id,
                        "reason": "shadow_confirmation_failed" if enough else "shadow_confirmation_timeout",
                        "evidence": evidence}))
            elif state.status == "active" and self._clock >= state.next_review:
                evidence = self._shadow_evidence(state, feedback)
                enough = evidence["sample_count"] >= self.config.min_confirmation_samples
                exit_required = (state.max_drawdown < -self.config.max_drawdown - COMPARISON_TOLERANCE
                                 or (enough and (evidence["return_score"]["score"] <= self.config.min_return
                                               or evidence["excess_score"]["score"] < self.config.min_parent_excess - COMPARISON_TOLERANCE)))
                if exit_required:
                    state.status = "retiring"
                    state.retire_at = self._clock + timedelta(days=self.config.activation_delay_days)
                    events.append(self._event("candidate_exit_scheduled", {"version_id": version.version_id,
                        "retire_at": state.retire_at.isoformat(), "reason": "predeclared_shadow_exit", "evidence": evidence}))
                state.next_review = self._clock + timedelta(days=self.config.research_every_days)
        self._cash_streak = self._cash_streak + 1 if selected_parent == "cash" else 0
        if self._next_research is not None and self._clock < self._next_research:
            return events
        self._next_research = self._clock + timedelta(days=self.config.research_every_days)
        if self._attempts >= min(self.config.max_attempts, len(self.config.candidate_plan)):
            if not self._budget_reported:
                self._budget_reported = True
                events.append(self._event("research_budget_exhausted", {"attempts": self._attempts,
                    "max_attempts": self.config.max_attempts, "plan_size": len(self.config.candidate_plan)}))
            return events
        mature = [s for s in feedback.samples if s.strategy in {spec.parent for spec in self.config.candidate_plan}
                  and datetime.fromisoformat(s.decision_date).date().toordinal() % (self.horizon_days + 1) == 0]
        mature_dates = {sample.decision_date for sample in mature}
        if self._cash_streak < self.config.trigger_cash_days or len(mature_dates) < self.config.min_mature_samples:
            events.append(self._event("research_waiting", {"reason": "insufficient_cash_streak_or_mature_evidence",
                "cash_streak": self._cash_streak, "mature_dates": len(mature_dates)}))
            return events
        for _ in range(self.config.attempts_per_cycle):
            if self._attempts >= min(self.config.max_attempts, len(self.config.candidate_plan)):
                break
            spec = self.config.candidate_plan[self._attempts]
            required = self.config.train_days + self.config.validation_days + spec.lookback_days
            if len(history.dates) < required:
                events.append(self._event("research_waiting", {"reason": "insufficient_visible_history",
                    "required_days": required, "visible_days": len(history.dates), "spec": asdict(spec)}))
                break
            self._attempts += 1
            attempt_id = hashlib.sha256(json.dumps({"spec": asdict(spec), "at": self._clock.isoformat(),
                "snapshot": history.content_hash, "attempt": self._attempts}, sort_keys=True).encode()).hexdigest()[:24]
            events.append(self._event("hypothesis_registered", {"attempt_id": attempt_id, "spec": asdict(spec),
                "parent_version": self.parent_version, "snapshot": history.content_hash,
                "cash_streak": self._cash_streak, "mature_evidence_ids": [sample.sample_id for sample in mature],
                "attempt": self._attempts, "max_attempts": self.config.max_attempts,
                "protocol": asdict(self.config), "rule": RESEARCH_RULE,
                "research_nature": "retrospective engineering candidate; future shadow required"}))
            try:
                evidence = evaluate(history, spec)
                parts = (evidence["train"], evidence["validation"])
                passed = all(part["candidate"]["total_return"] > self.config.min_return
                             and part["candidate"]["max_drawdown"] >= -self.config.max_drawdown - COMPARISON_TOLERANCE for part in parts)
                passed = passed and (evidence["validation"]["candidate"]["total_return"]
                    - evidence["validation"]["parent"]["total_return"] >= self.config.min_parent_excess - COMPARISON_TOLERANCE)
            except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
                events.append(self._event("research_attempt_failed", {"attempt_id": attempt_id,
                    "spec": asdict(spec), "reason": "evaluation_error", "error": str(exc)}))
                continue
            events.append(self._event("research_attempt_completed", {"attempt_id": attempt_id,
                "spec": asdict(spec), "passed": passed, "evidence": evidence,
                "reason": "internal_validation_passed" if passed else "internal_validation_failed"}))
            if not passed:
                continue
            payload = {"attempt_id": attempt_id, "spec": asdict(spec), "frozen_at": self._clock.isoformat(),
                       "parent_version": self.parent_version, "evidence": evidence}
            identifier = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
            version = CandidateVersion(identifier, spec, self._clock,
                self._clock + timedelta(days=self.config.shadow_delay_days), self.parent_version,
                json.dumps(evidence, sort_keys=True))
            self._states[identifier] = _CandidateState(version)
            events.append(self._event("candidate_frozen", {**version.record(), "attempt_id": attempt_id}))
        return events
