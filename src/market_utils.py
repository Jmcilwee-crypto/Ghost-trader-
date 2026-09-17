"""Small helpers for parsing Gamma/Data API quirks (several fields come back
as JSON-encoded strings instead of native arrays) shared by backtest.py and
live.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def parse_list_field(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [value]
    return [value]


def parse_timestamp(value: Any) -> float | None:
    """Best-effort parse of a timestamp field into unix seconds. Handles
    unix seconds, unix milliseconds, and ISO8601 strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        if value.isdigit():
            return parse_timestamp(int(value))
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def is_binary_market(market: dict) -> tuple[bool, list[str], list[str]]:
    """Returns (is_binary, clob_token_ids, outcomes)."""
    token_ids = parse_list_field(market.get("clobTokenIds"))
    outcomes = parse_list_field(market.get("outcomes"))
    ok = len(token_ids) == 2 and len(outcomes) == 2
    return ok, token_ids, outcomes


def winning_token_id(market: dict, token_ids: list[str]) -> str | None:
    """Heuristic: the resolved outcomePrices for a closed market settle to
    (1, 0) [or very close to it] for (winner, loser)."""
    prices = [float(p) for p in parse_list_field(market.get("outcomePrices")) if _is_number(p)]
    if len(prices) != len(token_ids) or not prices:
        return None
    best_idx = max(range(len(prices)), key=lambda i: prices[i])
    if prices[best_idx] < 0.9:
        return None  # doesn't look settled / resolved cleanly
    return token_ids[best_idx]


def _is_number(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def utcnow_ts() -> float:
    return datetime.now(timezone.utc).timestamp()
