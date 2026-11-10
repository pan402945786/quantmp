"""Content-addressed replay artifacts and a closed, non-executable JSON codec."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sys
import tempfile
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


CHECKPOINT_SCHEMA = "quantmp-checkpoint-v1"
CODE_FILES = ("research_system.py", "historical_replay.py", "strategy_selector.py", "strategy_research.py",
              "replay_artifacts.py", "frozen_replay.py", "replay_report.py", "replay.py", "research.py")


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def code_identity() -> dict:
    root = Path(__file__).parent
    hashes = {name: hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
              for name in CODE_FILES}
    return {"hash": digest(hashes), "files": hashes, "normalization": "CRLF to LF"}


def runtime_identity() -> dict:
    return {"python": sys.version, "implementation": platform.python_implementation(),
            "platform": sys.platform, "machine": platform.machine()}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def archive_code(directory: str | Path, identity: dict) -> str:
    if code_identity() != identity:
        raise ValueError("cannot archive changed replay code")
    target = Path(directory)
    source = Path(__file__).parent
    for name in CODE_FILES:
        content = (source / name).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
        write_immutable(target / name, content)
    write_immutable(target / "code-identity.json", canonical_json(identity) + "\n")
    return str(target.resolve())


def price_identity(panel, through: str | None = None) -> dict:
    dates = tuple(on for on in panel.dates if through is None or on <= through)
    hasher = hashlib.sha256()
    hasher.update(canonical_json({"assets": panel.assets}).encode())
    for on in dates:
        hasher.update(canonical_json([on, [panel.price(on, asset) for asset in panel.assets]]).encode())
        hasher.update(b"\n")
    return {"hash": hasher.hexdigest(), "days": len(dates), "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None, "assets": list(panel.assets)}


def write_immutable(path: str | Path, content: str) -> None:
    """Publish a complete artifact atomically, accepting only identical reruns."""
    target = Path(path)
    raw = content.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() == raw:
            return
        raise ValueError(f"artifact is immutable; choose a new output path: {target}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".quantmp-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError(f"artifact is immutable; choose a new output path: {target}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _types() -> tuple[dict, dict]:
    from historical_replay import ScheduledTargets
    from research_system import _PortfolioLedger
    from strategy_research import CandidateResearch, CandidateSpec, CandidateVersion, ResearchConfig, _CandidateState
    from strategy_selector import FeedbackBook, MatureFeedback, RegimeSelector, SelectorConfig, SelectorVersion
    records = {cls.__name__: cls for cls in (
        ScheduledTargets, _PortfolioLedger, CandidateSpec, CandidateVersion, ResearchConfig, _CandidateState,
        MatureFeedback, SelectorConfig, SelectorVersion)}
    managers = {
        "FeedbackBook": (FeedbackBook, {"horizon_days", "_delay", "_strategy_version", "_pending", "_entry_equities",
                                        "_samples", "_registered", "_clock"}),
        "RegimeSelector": (RegimeSelector, {"config", "_clock", "_next_research", "_versions", "_active"}),
        "CandidateResearch": (CandidateResearch, {"config", "horizon_days", "parent_version", "_clock", "_next_research",
                                                  "_cash_streak", "_attempts", "_states", "_budget_reported", "_active"}),
    }
    return records, managers


def encode_state(value):
    records, managers = _types()

    def encode(item):
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("checkpoint numbers must be finite")
            return item
        if isinstance(item, datetime):
            return {"type": "datetime", "value": item.isoformat()}
        if isinstance(item, timedelta):
            return {"type": "timedelta", "value": [item.days, item.seconds, item.microseconds]}
        if isinstance(item, list):
            return [encode(element) for element in item]
        if isinstance(item, (tuple, set)):
            values = [encode(element) for element in item]
            if isinstance(item, set):
                values.sort(key=canonical_json)
            return {"type": "tuple" if isinstance(item, tuple) else "set", "value": values}
        if isinstance(item, Mapping):
            return {"type": "mappingproxy" if isinstance(item, MappingProxyType) else "dict",
                    "value": [[encode(key), encode(element)] for key, element in item.items()]}
        name = type(item).__name__
        if is_dataclass(item) and name in records and type(item) is records[name]:
            return {"type": "record", "class": name,
                    "value": {field.name: encode(getattr(item, field.name)) for field in fields(item)}}
        if name in managers and type(item) is managers[name][0]:
            if set(item.__dict__) != managers[name][1]:
                raise ValueError(f"checkpoint manager fields changed: {name}")
            return {"type": "manager", "class": name, "value": encode(item.__dict__)}
        raise ValueError(f"unsupported checkpoint value: {type(item).__name__}")

    return encode(value)


def decode_state(value):
    records, managers = _types()

    def decode(item):
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("checkpoint numbers must be finite")
            return item
        if isinstance(item, list):
            return [decode(element) for element in item]
        if not isinstance(item, dict) or "type" not in item or "value" not in item:
            raise ValueError("invalid checkpoint value")
        tag = item["type"]
        allowed = {"type", "value", "class"} if tag in {"record", "manager"} else {"type", "value"}
        if set(item) != allowed:
            raise ValueError("invalid checkpoint value fields")
        body = item["value"]
        if tag == "datetime":
            from historical_replay import utc_timestamp
            return utc_timestamp(body)
        if tag == "timedelta":
            return timedelta(days=body[0], seconds=body[1], microseconds=body[2])
        if tag in {"tuple", "set"}:
            values = [decode(element) for element in body]
            return tuple(values) if tag == "tuple" else set(values)
        if tag in {"dict", "mappingproxy"}:
            result = {}
            for key, element in body:
                key = decode(key)
                if key in result:
                    raise ValueError("duplicate checkpoint mapping key")
                result[key] = decode(element)
            return MappingProxyType(result) if tag == "mappingproxy" else result
        if tag == "record" and item["class"] in records:
            cls = records[item["class"]]
            expected = {field.name for field in fields(cls)}
            if set(body) != expected:
                raise ValueError("checkpoint record fields changed")
            values = {name: decode(element) for name, element in body.items()}
            instance = cls(**{field.name: values[field.name] for field in fields(cls) if field.init})
            for field in fields(cls):
                if not field.init:
                    object.__setattr__(instance, field.name, values[field.name])
            return instance
        if tag == "manager" and item["class"] in managers:
            cls, expected = managers[item["class"]]
            state = decode(body)
            if not isinstance(state, dict) or set(state) != expected:
                raise ValueError("checkpoint manager fields changed")
            instance = cls.__new__(cls)
            instance.__dict__.update(state)
            if item["class"] == "CandidateResearch":
                # The active directory must refer to the same mutable states as
                # the lifecycle registry, including pending retirement.
                instance._active = {parent: instance._states[entry.version.version_id]
                                    for parent, entry in instance._active.items()}
            if item["class"] == "RegimeSelector" and instance._active is not None:
                instance._active = next(version for version in instance._versions
                                        if version.version_id == instance._active.version_id)
            return instance
        raise ValueError("unknown checkpoint type or class")

    try:
        return decode(value)
    except (KeyError, TypeError, IndexError, StopIteration, OverflowError) as exc:
        raise ValueError("malformed checkpoint state") from exc


def seal_checkpoint(payload: dict) -> dict:
    body = {"schema": CHECKPOINT_SCHEMA, **payload}
    return {**body, "checkpoint_hash": digest(body)}


def validate_checkpoint(checkpoint: dict) -> None:
    if not isinstance(checkpoint, dict) or checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        raise ValueError("unsupported checkpoint schema")
    body = {key: value for key, value in checkpoint.items() if key != "checkpoint_hash"}
    if checkpoint.get("checkpoint_hash") != digest(body):
        raise ValueError("checkpoint digest mismatch")
    manifest = checkpoint.get("manifest", {})
    identity = {key: value for key, value in manifest.items() if key != "run_id"}
    if manifest.get("run_id") != digest(identity):
        raise ValueError("frozen manifest digest mismatch")
    if manifest.get("code") != code_identity():
        raise ValueError("engine code changed; start a new frozen run")
    if manifest.get("runtime") != runtime_identity():
        raise ValueError("Python runtime changed; start a new frozen run")


def read_checkpoint(path: str | Path) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    checkpoint = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    validate_checkpoint(checkpoint)
    return checkpoint
