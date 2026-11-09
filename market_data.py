"""Download completed Binance spot daily closes for the local research loop."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"
DAY_MS = 24 * 60 * 60 * 1000


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _date_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp() * 1000)


def _close_text(value: object) -> str:
    try:
        close = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid close price: {value}") from exc
    if not close.is_finite() or close <= 0:
        raise ValueError(f"invalid close price: {value}")
    return format(close, "f")


def request_klines(symbol: str, start_ms: int, end_ms: int, timeout: float = 20.0) -> list[list]:
    query = urlencode({"symbol": symbol, "interval": "1d", "startTime": start_ms,
                       "endTime": end_ms, "limit": 1000})
    request = Request(BINANCE_KLINES_URL + "?" + query,
                      headers={"Accept": "application/json", "User-Agent": "quantmp/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError(f"Binance returned an error for {symbol}: {payload}")
    return payload


def fetch_daily_closes(symbol: str, start: str, end: str, timeout: float = 20.0) -> list[tuple[str, str, str]]:
    """Fetch [date, symbol, close] rows for completed UTC dates in [start, end)."""
    start_date = _parse_date(start, "start")
    end_date = _parse_date(end, "end")
    if start_date >= end_date:
        raise ValueError("start must be before end")
    start_ms = _date_ms(start_date)
    # A daily candle for the current UTC date is still forming until 00:00 UTC.
    end_ms = min(_date_ms(end_date), _date_ms(datetime.now(timezone.utc).date()))
    if start_ms >= end_ms:
        raise ValueError("requested range contains no completed UTC daily candle")
    rows: list[tuple[str, str, str]] = []
    while start_ms < end_ms:
        payload = request_klines(symbol, start_ms, end_ms - 1, timeout)
        if not payload:
            break
        for item in payload:
            if len(item) < 7:
                raise ValueError(f"unexpected Binance kline row for {symbol}: {item}")
            open_ms = int(item[0])
            if not start_ms <= open_ms < end_ms:
                continue
            on = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc).date().isoformat()
            rows.append((on, symbol, _close_text(item[4])))
        last_open_ms = int(payload[-1][0])
        next_start_ms = last_open_ms + DAY_MS
        if next_start_ms <= start_ms:
            raise ValueError(f"Binance pagination stalled for {symbol}")
        start_ms = next_start_ms
        if len(payload) < 1000:
            break
    if not rows:
        raise ValueError(f"no daily data returned for {symbol}")
    deduplicated = {row[0]: row for row in rows}
    return [deduplicated[on] for on in sorted(deduplicated)]


def write_price_panel(rows: list[tuple[str, str, str]], output: str | Path) -> dict:
    if not rows:
        raise ValueError("cannot write an empty price panel")
    ordered = sorted(set(rows), key=lambda row: (row[0], row[1]))
    dates = sorted({row[0] for row in ordered})
    assets = sorted({row[1] for row in ordered})
    expected = len(dates) * len(assets)
    if len(ordered) != expected:
        missing = [f"{on}/{asset}" for on in dates for asset in assets
                   if (on, asset) not in {(row[0], row[1]) for row in ordered}]
        raise ValueError("downloaded data is not rectangular; missing " + ", ".join(missing[:5]))
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("date", "asset", "close"))
        writer.writerows(ordered)
    content_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return {"path": str(output_path), "sha256": content_hash, "rows": len(ordered),
            "assets": assets, "start": dates[0], "end": dates[-1]}


def download_panel(symbols: list[str], start: str, end: str, output: str | Path,
                   timeout: float = 20.0) -> dict:
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must contain at least one unique symbol")
    rows = []
    for symbol in symbols:
        rows.extend(fetch_daily_closes(symbol, start, end, timeout))
    metadata = write_price_panel(rows, output)
    metadata.update({"source": BINANCE_KLINES_URL, "interval": "1d", "symbols": symbols,
                     "requested_start": start, "requested_end": end,
                     "downloaded_at": datetime.now(timezone.utc).isoformat()})
    manifest_path = Path(output).with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    metadata["manifest"] = str(manifest_path)
    return metadata
