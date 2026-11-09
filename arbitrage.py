"""Read-only cross-exchange spot order-book monitor."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

D = Decimal


def number(value, name, *, positive=False):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"invalid {name}: {value}")
    return result


@dataclass(frozen=True)
class Book:
    exchange: str
    bids: list
    asks: list
    received: float
    latency: float


def levels(rows, reverse):
    parsed = []
    for row in rows:
        price = number(row[0], "price", positive=True)
        amount = number(row[1], "amount", positive=True)
        parsed.append((price, amount))
    return sorted(parsed, key=lambda x: x[0], reverse=reverse)


def request_json(url, timeout):
    req = Request(url, headers={"User-Agent": "quantmp-arbitrage/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def fetch_book(exchange, timeout):
    kind, symbol = exchange["id"], exchange["symbol"]
    started = time.monotonic()
    if kind == "binance":
        data = request_json("https://api.binance.com/api/v3/depth?" + urlencode({"symbol": symbol, "limit": 100}), timeout)
        bids, asks = data["bids"], data["asks"]
    elif kind == "coinbase":
        from urllib.parse import quote
        data = request_json("https://api.exchange.coinbase.com/products/" + quote(symbol, safe="") + "/book?level=2", timeout)
        bids, asks = data["bids"], data["asks"]
    elif kind == "kraken":
        data = request_json("https://api.kraken.com/0/public/Depth?" + urlencode({"pair": symbol, "count": 100}), timeout)
        if data.get("error"):
            raise ValueError(", ".join(data["error"]))
        result = data["result"]
        if len(result) != 1:
            raise ValueError("unexpected Kraken order book response")
        book = next(iter(result.values()))
        bids, asks = book["bids"], book["asks"]
    else:
        raise ValueError(f"unsupported exchange: {kind}")
    received = time.monotonic()
    bids, asks = levels(bids, True), levels(asks, False)
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("empty or crossed order book")
    return Book(kind, bids, asks, received, received - started)


def fill(rows, quantity):
    remaining, total = quantity, D(0)
    for price, available in rows:
        taken = min(remaining, available)
        total += taken * price
        remaining -= taken
        if remaining == 0:
            return total
    return None


def evaluate(buy, sell, quantity, buy_fee, sell_fee, rebalance):
    cost = fill(buy.asks, quantity)
    proceeds = fill(sell.bids, quantity)
    if cost is None or proceeds is None:
        return None
    fees = cost * buy_fee + proceeds * sell_fee
    gross = proceeds - cost
    net = gross - fees - rebalance
    return {"buy": buy.exchange, "sell": sell.exchange, "quantity": quantity,
            "buy_vwap": cost / quantity, "sell_vwap": proceeds / quantity,
            "gross": gross, "fees": fees, "rebalance": rebalance, "net": net,
            "net_bps": net / cost * D(10000)}


def validate(config):
    quantity = number(config["quantity"], "quantity", positive=True)
    timeout = float(number(config.get("timeout_seconds", 5), "timeout_seconds", positive=True))
    interval = float(number(config.get("interval_seconds", 10), "interval_seconds", positive=True))
    max_age = float(number(config.get("max_age_seconds", 3), "max_age_seconds", positive=True))
    max_skew = float(number(config.get("max_skew_seconds", 2), "max_skew_seconds", positive=True))
    rebalance = number(config.get("rebalance_cost_quote", 0), "rebalance_cost_quote")
    threshold = number(config.get("min_net_profit_quote", 0), "min_net_profit_quote")
    exchanges = config["exchanges"]
    if not isinstance(exchanges, list) or len(exchanges) < 2:
        raise ValueError("at least two exchanges are required")
    seen = set()
    for item in exchanges:
        name = item["id"]
        if name not in {"binance", "coinbase", "kraken"} or name in seen:
            raise ValueError(f"unsupported or duplicate exchange: {name}")
        if not isinstance(item.get("symbol"), str) or not item["symbol"].strip():
            raise ValueError(f"missing symbol for {name}")
        fee = number(item["taker_fee"], f"{name} taker_fee")
        if fee >= 1:
            raise ValueError("taker_fee must be a decimal fraction below 1")
        seen.add(name)
    return quantity, timeout, interval, max_age, max_skew, rebalance, threshold


def setup_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS observations (
        observed_at TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL,
        buy_exchange TEXT NOT NULL, sell_exchange TEXT NOT NULL,
        quantity TEXT NOT NULL, buy_vwap TEXT NOT NULL, sell_vwap TEXT NOT NULL,
        gross TEXT NOT NULL, fees TEXT NOT NULL, rebalance TEXT NOT NULL,
        net TEXT NOT NULL, net_bps TEXT NOT NULL, qualified INTEGER NOT NULL,
        buy_age_ms REAL NOT NULL, sell_age_ms REAL NOT NULL, skew_ms REAL NOT NULL,
        buy_latency_ms REAL NOT NULL, sell_latency_ms REAL NOT NULL
    )""")
    db.commit()
    return db


def scan(config, db, *, demo=False):
    quantity, timeout, _, max_age, max_skew, rebalance, threshold = validate(config)
    settings = {e["id"]: e for e in config["exchanges"]}
    books, errors = {}, []
    if demo:
        now = time.monotonic()
        examples = [("100000", "100010"), ("100120", "100130"), ("100080", "100090")]
        for item, (bid, ask) in zip(config["exchanges"], examples):
            books[item["id"]] = Book(item["id"], [(D(bid), D(10))], [(D(ask), D(10))], now, 0)
    else:
        with ThreadPoolExecutor(max_workers=len(settings)) as pool:
            futures = {pool.submit(fetch_book, item, timeout): item["id"] for item in config["exchanges"]}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    books[name] = future.result()
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
    now = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = []
    for buy in books.values():
        for sell in books.values():
            if buy.exchange == sell.exchange:
                continue
            buy_age, sell_age = now - buy.received, now - sell.received
            skew = abs(buy.received - sell.received)
            if max(buy_age, sell_age, buy.latency, sell.latency) > max_age or skew > max_skew:
                errors.append(f"{buy.exchange}->{sell.exchange}: stale, slow or unsynchronized books")
                continue
            result = evaluate(buy, sell, quantity, number(settings[buy.exchange]["taker_fee"], "fee"),
                              number(settings[sell.exchange]["taker_fee"], "fee"), rebalance)
            if result is None:
                errors.append(f"{buy.exchange}->{sell.exchange}: insufficient depth")
                continue
            qualified = result["net"] > 0 and result["net"] >= threshold
            db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                timestamp, config["base"], config["quote"], result["buy"], result["sell"],
                *(str(result[k]) for k in ("quantity", "buy_vwap", "sell_vwap", "gross", "fees", "rebalance", "net", "net_bps")),
                int(qualified), buy_age * 1000, sell_age * 1000, skew * 1000,
                buy.latency * 1000, sell.latency * 1000))
            rows.append((result, qualified))
    db.commit()
    rows.sort(key=lambda row: row[0]["net"], reverse=True)
    print(f"\n{timestamp} {config['base']}/{config['quote']} quantity={quantity}")
    for result, qualified in rows:
        print(f"{'SIGNAL' if qualified else '      '} {result['buy']:8} -> {result['sell']:8} "
              f"buy={result['buy_vwap']:.4f} sell={result['sell_vwap']:.4f} "
              f"gross={result['gross']:.4f} fees={result['fees']:.4f} "
              f"rebalance={result['rebalance']:.4f} net={result['net']:.4f} "
              f"({result['net_bps']:.2f} bps)")
    for error in errors:
        print(f"WARN {error}", file=sys.stderr)
    if not rows:
        print("No comparable order books.")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--demo", action="store_true", help="use fixed sample books, without network")
    args = parser.parse_args()
    try:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        if not config.get("base") or not config.get("quote"):
            raise ValueError("base and quote are required")
        _, _, interval, _, _, _, _ = validate(config)
        with setup_db(":memory:" if args.demo else config.get("database", "arbitrage.sqlite3")) as db:
            while True:
                started = time.monotonic()
                scan(config, db, demo=args.demo)
                if args.once:
                    break
                time.sleep(max(0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
