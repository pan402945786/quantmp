"""Freeze a replay protocol and continue actual simulator state from JSON."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import sqlite3

from historical_replay import PROTOCOL_VERSION, utc_timestamp
from replay_artifacts import (
    canonical_json, code_identity, decode_state, digest, encode_state, now_utc,
    price_identity, runtime_identity, seal_checkpoint, validate_checkpoint, write_immutable,
)
from research_system import (
    DEFAULT_STRATEGY_BUDGETS, MIX_RULE, STRATEGIES, VERSION, _simulate, _validate_budgets,
    _validate_config, save_experiment, setup_research_db,
)
from strategy_research import RESEARCH_RULE, ResearchConfig
from strategy_selector import SELECTOR_RULE, SelectorConfig


MODES = ("standalone", "portfolio", "selector", "research")


def frozen_protocol(config: dict, mode: str = "research", strategies=None) -> dict:
    if mode not in MODES:
        raise ValueError("unknown frozen replay mode")
    names = list(strategies) if strategies is not None else list(STRATEGIES)
    if not names or len(set(names)) != len(names) or any(name not in STRATEGIES for name in names):
        raise ValueError("frozen replay requires unique known strategies")
    if mode != "standalone" and names != list(STRATEGIES):
        raise ValueError("portfolio modes require the complete original shadow library")
    cfg = _validate_config(config)
    for key in ("start_at", "end_at"):
        cfg[key] = cfg[key].isoformat() if cfg[key] is not None else None
    if mode != "standalone":
        cfg["strategy_budgets"] = _validate_budgets(config.get("strategy_budgets", DEFAULT_STRATEGY_BUDGETS), names)
    if mode in {"selector", "research"}:
        selector = SelectorConfig.parse(config.get("selector", {}), names)
        cfg["selector"] = asdict(selector)
    if mode == "research":
        cfg["research"] = asdict(ResearchConfig.parse(config.get("research", {}), selector.candidates))
    # Roundtrip normalizes immutable tuples and JSON lists to one protocol.
    import json
    cfg = json.loads(canonical_json(cfg))
    return {"mode": mode, "strategies": names, "config": cfg,
            "rules": {"engine": VERSION, "execution": PROTOCOL_VERSION, "fixed_mix": MIX_RULE,
                      "selector": SELECTOR_RULE, "research": RESEARCH_RULE}}


def _validate_state(state: dict, protocol: dict) -> None:
    expected = {"accounts", "selector", "feedback", "research", "extra_feedback", "last_date", "as_of", "history_hash"}
    if not isinstance(state, dict) or set(state) != expected or not state["accounts"]:
        raise ValueError("invalid checkpoint simulator state")
    cfg = protocol["config"]
    if len({ledger.name for ledger in state["accounts"]}) != len(state["accounts"]):
        raise ValueError("duplicate checkpoint account")
    for ledger in state["accounts"]:
        if not ledger.portfolio or ledger.portfolio[-1]["date"] != state["last_date"]:
            raise ValueError("checkpoint account clocks do not agree")
        if utc_timestamp(ledger.portfolio[-1]["as_of"]) != state["as_of"]:
            raise ValueError("checkpoint account availability times do not agree")
        if ledger.config["initial_capital"] != cfg["initial_capital"] or ledger.config["max_weight"] != cfg["max_weight"]:
            raise ValueError("checkpoint account protocol changed")
        if ledger.cash < 0 or any(value < 0 for value in ledger.units.values()):
            raise ValueError("checkpoint contains a borrowed or short position")
        if ledger.pending is None or ledger.pending.submitted_at != state["as_of"]:
            raise ValueError("checkpoint must preserve the latest pending order")
        if ledger.pending.execution_at <= state["as_of"]:
            raise ValueError("checkpoint pending order is already overdue")
    needs_selector = protocol["mode"] in {"selector", "research"}
    if needs_selector != (state["selector"] is not None and state["feedback"] is not None):
        raise ValueError("checkpoint selector mode changed")
    needs_research = protocol["mode"] == "research"
    if needs_research != (state["research"] is not None and state["extra_feedback"] is not None):
        raise ValueError("checkpoint research mode changed")
    for key in ("selector", "feedback", "research", "extra_feedback"):
        if state[key] is not None and state[key]._clock != state["as_of"]:
            raise ValueError("checkpoint module clock changed")


def run_frozen(panel, config: dict | None = None, mode: str | None = None, strategies=None,
               checkpoint: dict | None = None, until=None):
    """Return a new immutable checkpoint and cumulative results.

    `until` limits processing without changing the frozen protocol. Resume
    reconstructs only the released price view; no past strategy call repeats.
    """
    wall_clock = now_utc()
    if checkpoint is None:
        if config is None:
            raise ValueError("a new frozen run requires a configuration")
        protocol = frozen_protocol(config, mode or "research", strategies)
        identity = {"created_at": wall_clock.isoformat(), "code": code_identity(), "runtime": runtime_identity(),
                    "protocol": protocol, "origin_data": price_identity(panel),
                    "origin_source": str(Path(panel.source).resolve()) if Path(panel.source).is_file() else panel.source,
                    "origin_raw_hash": panel.content_hash,
                    "data_vintage": "historical CSV snapshot; actual publication and collection times unknown"}
        manifest = {**identity, "run_id": digest(identity)}
        state = None
    else:
        validate_checkpoint(checkpoint)
        manifest = checkpoint["manifest"]
        protocol = manifest["protocol"]
        if mode is not None and mode != protocol["mode"]:
            raise ValueError("frozen mode changed; start a new run")
        if strategies is not None and list(strategies) != protocol["strategies"]:
            raise ValueError("frozen strategy library changed; start a new run")
        if config is not None and frozen_protocol(config, protocol["mode"], protocol["strategies"]) != protocol:
            raise ValueError("frozen parameters changed; start a new run")
        known = checkpoint["known_data"]
        if price_identity(panel, known["last_date"]) != known:
            raise ValueError("previously known prices or asset universe changed; only append new daily rows")
        state = decode_state(checkpoint["state"])
        _validate_state(state, protocol)
    cfg = protocol["config"]
    names = protocol["strategies"]
    mode = protocol["mode"]
    budgets = cfg.get("strategy_budgets")
    selector = SelectorConfig.parse(cfg["selector"], names) if mode in {"selector", "research"} else None
    research = ResearchConfig.parse(cfg["research"], selector.candidates) if mode == "research" else None
    requested = utc_timestamp(until) if until is not None else wall_clock
    stop = min(requested, wall_clock)
    latest = []
    results = _simulate(panel, cfg, names, budgets, selector, research, resume_state=state,
                        checkpoint_callback=lambda current: latest.__setitem__(slice(None), [current]), stop_at=stop)
    state = latest[0]
    known = price_identity(panel)
    if checkpoint is not None and state["as_of"].isoformat() == checkpoint["as_of"] and known == checkpoint["known_data"]:
        return checkpoint, results
    payload = {"manifest": manifest, "parent_hash": checkpoint["checkpoint_hash"] if checkpoint else "",
               "as_of": state["as_of"].isoformat(), "last_date": state["last_date"],
               "known_data": known,
               "data_source": str(Path(panel.source).resolve()) if Path(panel.source).is_file() else panel.source,
               "state": encode_state(state)}
    return seal_checkpoint(payload), results


def persist_frozen(database: str | Path, checkpoint: dict, panel, results) -> str:
    """Append a revision; one parent cannot silently fork into two continuations."""
    validate_checkpoint(checkpoint)
    manifest = checkpoint["manifest"]
    checkpoint_hash = checkpoint["checkpoint_hash"]
    identifier = digest({"run_id": manifest["run_id"], "checkpoint": checkpoint_hash})[:16]
    config = {**manifest["protocol"]["config"], "frozen_run_id": manifest["run_id"],
              "checkpoint_hash": checkpoint_hash, "parent_checkpoint": checkpoint["parent_hash"],
              "code_hash": manifest["code"]["hash"], "snapshot_as_of": checkpoint["as_of"]}
    db = setup_research_db(database)
    try:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS frozen_runs (
            run_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS frozen_checkpoints (
            checkpoint_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_hash TEXT NOT NULL,
            as_of TEXT NOT NULL, experiment_id TEXT NOT NULL, known_data_json TEXT NOT NULL,
            UNIQUE (run_id, parent_hash)
        );
        """)
        with db:
            prior = db.execute("SELECT manifest_json FROM frozen_runs WHERE run_id=?", (manifest["run_id"],)).fetchone()
            frozen_json = canonical_json(manifest)
            if prior is None:
                db.execute("INSERT INTO frozen_runs VALUES (?,?)", (manifest["run_id"], frozen_json))
            elif prior[0] != frozen_json:
                raise ValueError("frozen run manifest is immutable")
            values = (checkpoint_hash, manifest["run_id"], checkpoint["parent_hash"], checkpoint["as_of"],
                      identifier, canonical_json(checkpoint["known_data"]))
            existing = db.execute("SELECT * FROM frozen_checkpoints WHERE checkpoint_hash=?", (checkpoint_hash,)).fetchone()
            if existing is None:
                parent = checkpoint["parent_hash"]
                if parent and db.execute("SELECT run_id FROM frozen_checkpoints WHERE checkpoint_hash=?", (parent,)).fetchone() != (manifest["run_id"],):
                    raise ValueError("persist the parent checkpoint in this database before continuing")
                sibling = db.execute("SELECT checkpoint_hash FROM frozen_checkpoints WHERE run_id=? AND parent_hash=?",
                                     (manifest["run_id"], parent)).fetchone()
                if sibling is not None:
                    raise ValueError("checkpoint parent already has a continuation; resume the latest checkpoint")
                db.execute("INSERT INTO frozen_checkpoints VALUES (?,?,?,?,?,?)", values)
            elif existing != values:
                raise ValueError("checkpoint metadata is immutable")
            semantic_panel = replace(panel, content_hash=checkpoint["known_data"]["hash"])
            save_experiment(db, identifier, semantic_panel, config, results)
    except sqlite3.IntegrityError as exc:
        raise ValueError("frozen checkpoint persistence conflict") from exc
    finally:
        db.close()
    return identifier


def save_checkpoint(path: str | Path, checkpoint: dict) -> None:
    validate_checkpoint(checkpoint)
    write_immutable(path, canonical_json(checkpoint) + "\n")
