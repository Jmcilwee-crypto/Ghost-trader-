"""Thin read-only wrappers around Polymarket's public APIs.

Three hosts, no auth required for any of this (auth is only needed to place
real orders, which this project never does):

  - Gamma API   https://gamma-api.polymarket.com   market metadata/discovery
  - Data API    https://data-api.polymarket.com     trade & activity history
  - CLOB API    https://clob.polymarket.com         order book / price history

Endpoint shapes here are based on Polymarket's published docs as of the time
this was written. If a field is missing/renamed on their end, that will show
up as a KeyError/empty response rather than silently misbehaving -- check the
live JSON with `python -m src.polymarket_client selftest` if something breaks.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ghost-trader/0.1 (paper-trading research bot)"})


def _get(url: str, params: dict | None = None, retries: int = 3, timeout: float = 10.0) -> Any:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}")


class PolymarketClient:
    """Read-only client. Every method returns plain dicts/lists (raw JSON)."""

    # ---- Gamma: market discovery -----------------------------------------
    def list_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str = "volume",
        ascending: bool = False,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "offset": offset, "order": order, "ascending": str(ascending).lower()}
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        return _get(f"{GAMMA_BASE}/markets", params=params)

    def list_closed_markets(self, *, limit: int = 50, min_volume: float = 0.0) -> list[dict]:
        markets = self.list_markets(active=False, closed=True, limit=limit, order="volume", ascending=False)
        if min_volume:
            markets = [m for m in markets if _safe_float(m.get("volume")) >= min_volume]
        return markets

    def get_market_by_condition_id(self, condition_id: str) -> dict | None:
        """Look a market up whether it's still open or already settled.

        Gamma silently excludes closed markets from the default query, so a
        plain lookup returns nothing the moment a market resolves. Relying on
        that alone means resolutions are never detected and positions hang
        open forever -- so if the first query comes back empty, ask again for
        closed markets explicitly. Open markets (the common case while
        ingesting trades) still cost a single request.
        """
        for params in ({"condition_ids": condition_id},
                       {"condition_ids": condition_id, "closed": "true"}):
            results = _get(f"{GAMMA_BASE}/markets", params=params)
            if results:
                return results[0]
        return None

    # ---- Data API: trades & activity --------------------------------------
    def get_trades(
        self,
        *,
        user: str | None = None,
        market: str | Iterable[str] | None = None,
        limit: int = 100,
        taker_only: bool = True,
        side: str | None = None,
    ) -> list[dict]:
        """Fetch trades, most recent first.

        Omitting `user` returns the global public trade feed (used by
        Polymarket's own activity page); passing `market` (a condition id,
        or comma-separated list of them) scopes it to specific market(s).
        """
        params: dict[str, Any] = {"limit": limit, "takerOnly": str(taker_only).lower()}
        if user:
            params["user"] = user
        if market:
            params["market"] = market if isinstance(market, str) else ",".join(market)
        if side:
            params["side"] = side
        return _get(f"{DATA_BASE}/trades", params=params)

    # ---- CLOB: live order book & price history -----------------------------
    def get_order_book(self, token_id: str) -> dict:
        return _get(f"{CLOB_BASE}/book", params={"token_id": token_id})

    def get_midpoint(self, token_id: str) -> float | None:
        data = _get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        return _safe_float(data.get("mid")) if isinstance(data, dict) else None

    def get_price_history(self, token_id: str, *, interval: str = "1d", fidelity: int = 10) -> list[dict]:
        data = _get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        return data.get("history", []) if isinstance(data, dict) else data


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        client = PolymarketClient()
        print("Fetching a few active markets...")
        markets = client.list_markets(active=True, closed=False, limit=3)
        print(json.dumps(markets, indent=2)[:2000])
        print("\nFetching recent global trades...")
        trades = client.get_trades(limit=5)
        print(json.dumps(trades, indent=2)[:2000])
    else:
        print("Usage: python -m src.polymarket_client selftest")
