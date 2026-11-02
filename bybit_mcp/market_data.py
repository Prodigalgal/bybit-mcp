"""Bounded, public-only market research snapshots with per-component results."""
import asyncio
from datetime import datetime, timedelta, timezone
import time

from jsonschema import Draft202012Validator

from .bybit import BybitError, HOSTS, market_environment, response_context
from .catalog import BY_PATH
from .security import enforce


INTERVALS = ["1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"]
SNAPSHOT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["symbols"],
    "properties": {
        "symbols": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z0-9_.-]{1,100}$"},
                    "minItems": 1, "maxItems": 8, "uniqueItems": True},
        "category": {"type": "string", "enum": ["linear", "inverse", "spot"], "default": "linear"},
        "intervals": {"type": "array", "items": {"type": "string", "enum": INTERVALS},
                      "minItems": 1, "maxItems": 3, "uniqueItems": True, "default": ["60", "D"]},
        "kline_limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 60},
        "include": {"type": "array", "items": {"type": "string", "enum": ["instruments", "open_interest", "funding", "orderbook"]},
                    "maxItems": 4, "uniqueItems": True, "default": ["instruments", "open_interest", "funding"]},
        "open_interest_interval": {"type": "string", "enum": ["5min", "15min", "30min", "1h", "4h", "1d"], "default": "1h"},
        "open_interest_limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 24},
        "funding_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
        "closed_klines_only": {"type": "boolean", "default": True},
    },
}
PATHS = {"ticker": "/v5/market/tickers", "instruments": "/v5/market/instruments-info",
         "klines": "/v5/market/kline", "open_interest": "/v5/market/open-interest",
         "funding": "/v5/market/funding/history", "orderbook": "/v5/market/orderbook"}


def candle_end(start_ms, interval):
    if interval.isdigit():
        return start_ms + int(interval) * 60_000
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    if interval == "M":
        end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1, day=1)
    else:
        end = start + timedelta(days=1 if interval == "D" else 7)
    return int(end.timestamp() * 1000)


async def market_snapshot(client, profile, arguments, scopes):
    errors = list(Draft202012Validator(SNAPSHOT_SCHEMA).iter_errors(arguments))
    if errors:
        raise ValueError("Invalid snapshot parameter: " + errors[0].message)
    options = {name: spec["default"] for name, spec in SNAPSHOT_SCHEMA["properties"].items() if "default" in spec}
    options.update(arguments)
    category = options["category"]
    source = market_environment(profile["environment"])
    started = int(time.time() * 1000)
    items = {symbol: {"symbol": symbol, "klines": {}, "errors": {}} for symbol in options["symbols"]}
    jobs = []
    async def collect(symbol, component, params, interval=None):
        item = items[symbol]
        key = "klines." + interval if interval else component
        try:
            endpoint = BY_PATH[("GET", PATHS[component])]
            # Use the normal client on every request, retaining the connection,
            # scope/operation policy, revocation checks, pacing and host routing.
            response = await client.request(profile, endpoint, params, scopes)
            result = response["data"]["result"]
            as_of = response["data"].get("time")
            if component == "klines":
                rows = sorted(result.get("list", []), key=lambda row: int(row[0]))
                excluded = 0
                if options["closed_klines_only"]:
                    if as_of is None:
                        raise ValueError("Exchange timestamp missing; cannot determine closed candles")
                    closed = [row for row in rows if candle_end(int(row[0]), interval) <= int(as_of)]
                    excluded = len(rows) - len(closed)
                    rows = closed
                rows = rows[-options["kline_limit"]:]
                item["klines"][interval] = {
                    "list": rows, "as_of_ms": as_of, "closed_only": options["closed_klines_only"],
                    "excluded_open_candles": excluded,
                    "next_end_ms": int(rows[0][0]) - 1 if rows else None,
                }
            elif component in {"ticker", "instruments"}:
                value = next((row for row in result.get("list", []) if row.get("symbol") == symbol), None)
                if value is None:
                    raise ValueError("Exact symbol not returned by Bybit: " + symbol)
                item[component] = {"as_of_ms": as_of, "data": value}
            elif component in {"open_interest", "funding"}:
                time_key = "timestamp" if component == "open_interest" else "fundingRateTimestamp"
                item[component] = {"as_of_ms": as_of,
                                   "list": sorted(result.get("list", []), key=lambda row: int(row[time_key])),
                                   "nextPageCursor": result.get("nextPageCursor", "")}
            else:
                item[component] = {"as_of_ms": as_of, "data": result}
        except (BybitError, ValueError, PermissionError) as error:
            item["errors"][key] = {"error": str(error), "code": getattr(error, "code", None)}
        except Exception:
            client.store.audit(profile["id"], "market_snapshot." + key, "internal_error")
            item["errors"][key] = {"error": "Market component unavailable", "code": None}

    for symbol, item in items.items():
        params = {"category": category, "symbol": symbol}
        jobs.append(collect(symbol, "ticker", params))
        for interval in options["intervals"]:
            jobs.append(collect(symbol, "klines", {**params, "interval": interval,
                        "limit": options["kline_limit"] + int(options["closed_klines_only"])}, interval))
        for component in options["include"]:
            if category == "spot" and component in {"open_interest", "funding"}:
                item.setdefault("unavailable", {})[component] = "Not applicable to spot"
                continue
            extra = {}
            if component == "open_interest":
                extra = {"intervalTime": options["open_interest_interval"], "limit": options["open_interest_limit"]}
            elif component == "funding":
                extra = {"limit": options["funding_limit"]}
            elif component == "orderbook":
                extra = {"limit": 25}
            jobs.append(collect(symbol, component, {**params, **extra}))
    await asyncio.gather(*jobs)
    enforce(profile, scope="bybit:read")
    error_count = sum(len(item["errors"]) for item in items.values())
    return {**response_context(profile["environment"], source, HOSTS[source], public=True, public_only=profile.get("public_only", False)),
            "started_at_ms": started, "category": category, "partial": bool(error_count), "error_count": error_count,
            "state": "complete" if not error_count else "partial" if any("ticker" in item for item in items.values()) else "failed",
            "kline_columns": ["startTime_ms", "open", "high", "low", "close", "volume", "turnover"],
            "items": list(items.values()),
            "note": "Public snapshots are collected concurrently, not atomically. Prices/quantities retain V5 strings; series are ascending. D/W/M candles use UTC and include contract weekend trading. next_end_ms/nextPageCursor can continue history."}
