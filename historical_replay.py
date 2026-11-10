"""UTC daily-bar replay with detached, immutable, point-in-time snapshots.

The replay owns unreleased data. A HistoryView owns only released rows and has
no reference back to the replay, full panel, file path, or full-file digest.
This is an application boundary, not a sandbox for arbitrary Python code.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Mapping


PROTOCOL_VERSION = "daily-close-next-close-v1"
UTC = timezone.utc


def utc_timestamp(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("replay timestamps must include a timezone")
    return parsed.astimezone(UTC)


def daily_close(on: str) -> datetime:
    """The close labelled D occurs at the start of D+1, never at D's start."""
    return datetime.combine(date.fromisoformat(on) + timedelta(days=1), time(), UTC)


def validate_delay(seconds: float) -> float:
    seconds = float(seconds)
    if not math.isfinite(seconds) or not 0 <= seconds < 86400:
        raise ValueError("data_delay_seconds must be in [0, 86400) for next-close execution")
    return seconds


class UnavailableDataError(ValueError):
    """A requested row is outside the released snapshot."""


@dataclass(frozen=True, slots=True)
class BarTiming:
    event_at: datetime
    available_at: datetime
    published_at: datetime | None = None
    collected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HistoryView:
    as_of: datetime
    dates: tuple[str, ...]
    assets: tuple[str, ...]
    prices: Mapping[str, Mapping[str, float]]
    timings: Mapping[str, BarTiming]
    content_hash: str

    def price(self, on: str, asset: str) -> float:
        if on not in self.prices:
            raise UnavailableDataError(f"price date {on} is not available in this snapshot")
        return self.prices[on][asset]

    def timing(self, on: str) -> BarTiming:
        if on not in self.timings:
            raise UnavailableDataError(f"price date {on} is not available in this snapshot")
        return self.timings[on]


class DailyReplay:
    def __init__(self, dates: tuple[str, ...], prices: Mapping[str, Mapping[str, float]],
                 data_delay_seconds: float = 0):
        self._delay = timedelta(seconds=validate_delay(data_delay_seconds))
        self._dates = tuple(dates)
        if self._dates != tuple(sorted(set(self._dates))):
            raise ValueError("replay dates must be sorted and unique")
        # Own every row; neither later input mutations nor strategy mutations
        # can change a released snapshot or the simulator's future prices.
        self._rows = {on: MappingProxyType(dict(prices[on])) for on in self._dates}
        self._cursor = 0
        self._clock: datetime | None = None
        self._visible: dict[str, Mapping[str, float]] = {}
        self._timings: dict[str, BarTiming] = {}
        self._digest = hashlib.sha256(PROTOCOL_VERSION.encode())

    def available_at(self, on: str) -> datetime:
        return daily_close(on) + self._delay

    def advance_to(self, as_of: str | datetime) -> HistoryView:
        clock = utc_timestamp(as_of)
        if self._clock is not None and clock < self._clock:
            raise ValueError("historical clock cannot move backwards")
        self._clock = clock
        while self._cursor < len(self._dates):
            on = self._dates[self._cursor]
            available = self.available_at(on)
            if available > clock:
                break
            self._visible[on] = self._rows[on]
            self._timings[on] = BarTiming(daily_close(on), available)
            payload = {"date": on, "prices": dict(self._rows[on]), "available_at": available.isoformat()}
            self._digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
            self._digest.update(b"\n")
            self._cursor += 1
        visible_dates = tuple(self._visible)
        assets = tuple(sorted({asset for row in self._visible.values() for asset in row}))
        return HistoryView(clock, visible_dates, assets, MappingProxyType(dict(self._visible)),
                           MappingProxyType(dict(self._timings)), self._digest.hexdigest())


@dataclass(frozen=True, slots=True)
class ScheduledTargets:
    decision_date: str
    submitted_at: datetime
    execution_date: str
    targets: Mapping[str, float]

    def __post_init__(self):
        submitted = utc_timestamp(self.submitted_at)
        if daily_close(self.execution_date) <= submitted:
            raise ValueError("orders must be submitted before their execution close")
        object.__setattr__(self, "submitted_at", submitted)
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    @property
    def execution_at(self) -> datetime:
        return daily_close(self.execution_date)

    def event(self, kind: str, processed_at: datetime) -> dict:
        clock = utc_timestamp(processed_at)
        if clock < self.submitted_at:
            raise ValueError("order events cannot precede submission")
        if kind == "filled" and clock < self.execution_at:
            raise ValueError("orders cannot fill before their execution close")
        if kind not in {"submitted", "filled", "pending"}:
            raise ValueError("unknown order event")
        return {"decision_date": self.decision_date, "event": kind,
                "event_at": (self.execution_at if kind == "filled" else clock).isoformat(),
                "processed_at": clock.isoformat(), "execution_date": self.execution_date,
                "execution_at": self.execution_at.isoformat()}
