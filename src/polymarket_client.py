"""Thin read-only wrappers around Polymarket's public APIs.

Three hosts, no auth required for any of this (auth is only needed to place
real orders, which this project never does):

  - Gamma API   https://gamma-api.polymarket.com   market metadata/discovery
  - Data API    https://data-api.polymarket.com/v2  trade & activity history
                (v1 was retired 2 Nov 2026 and now answers 410)
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

from .market_utils import parse_timestamp

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
DATA_V2 = f"{DATA_BASE}/v2"
CLOB_BASE = "https://clob.polymarket.com"

# Data API v1 was retired on 2 Nov 2026. It now answers 410 with a pointer to
# v2, which the Polymarket experiment ran into head-first: the cycle failed
# fast, the workflow's `|| echo continuing` swallowed it, the four spot
# experiments kept committing state, and the fleet sat dead for two days
# looking exactly like a quiet market.
#
# v2 changed four things at once: every field is snake_case, the rows arrive
# wrapped in {"data", "pagination"}, `offset` is refused outright in favour of
# opaque cursors, and `limit` is capped at 1000. Only the names are worth
# propagating, and experiment.py, live.py and backtest.py all read the v1
# spelling, so the rename is absorbed here instead of in three modules.
_TRADE_ALIASES = {
    "proxy_wallet": "proxyWallet",
    "condition_id": "conditionId",
    "token_id": "asset",
    "transaction_hash": "transactionHash",
}
# Carried through unrenamed. `title` is new and free -- v1 needed a separate
# Gamma lookup for it. The dropped fields (icon, bio, profile_image*,
# pseudonym) are long strings nothing reads, and a cycle now holds tens of
# thousands of these rows at once.
_TRADE_KEEP = ("timestamp", "price", "size", "outcome", "side", "title", "slug")


def _normalize_trade(row: dict) -> dict:
    """v2 field names -> the v1 vocabulary the rest of the project speaks."""
    out = {alias: row.get(src) for src, alias in _TRADE_ALIASES.items()}
    for key in _TRADE_KEEP:
        if key in row:
            out[key] = row[key]
    return out

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
    MAX_TRADE_LIMIT = 1000      # v2 refuses anything larger

    def _trades_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        user: str | None = None,
        market: str | Iterable[str] | None = None,
        taker_only: bool = True,
        side: str | None = None,
    ) -> tuple[list[dict], str | None, bool]:
        """One page of the feed, as (trades, next_cursor, has_more)."""
        params: dict[str, Any] = {"limit": min(limit, self.MAX_TRADE_LIMIT),
                                  "takerOnly": str(taker_only).lower()}
        if cursor:
            params["cursor"] = cursor
        if user:
            params["user"] = user
        if market:
            params["market"] = market if isinstance(market, str) else ",".join(market)
        if side:
            params["side"] = side
        payload = _get(f"{DATA_V2}/trades", params=params)

        # Tolerate a bare list in case the envelope is ever dropped again --
        # losing the cursor costs coverage, not correctness.
        if isinstance(payload, list):
            return [_normalize_trade(r) for r in payload], None, False
        if not isinstance(payload, dict):
            return [], None, False
        rows = payload.get("data") or []
        page = payload.get("pagination") or {}
        return ([_normalize_trade(r) for r in rows],
                page.get("next_cursor"), bool(page.get("has_more")))

    def get_trades(
        self,
        *,
        user: str | None = None,
        market: str | Iterable[str] | None = None,
        limit: int = 100,
        cursor: str | None = None,
        taker_only: bool = True,
        side: str | None = None,
    ) -> list[dict]:
        """Fetch trades, most recent first.

        Omitting `user` returns the global public trade feed (used by
        Polymarket's own activity page); passing `market` (a condition id,
        or comma-separated list of them) scopes it to specific market(s).
        To walk further back, pass the `cursor` from a previous response --
        or use get_trades_since, which does that walk for you.
        """
        trades, _, _ = self._trades_page(
            limit=limit, cursor=cursor, user=user, market=market,
            taker_only=taker_only, side=side)
        return trades

    def get_trades_since(
        self,
        since_ts: float,
        *,
        page_size: int = 1000,
        max_pages: int = 60,
        max_lookback_seconds: float = 6 * 3600.0,
    ) -> tuple[list[dict], bool]:
        """Walk the global feed backwards until it reaches `since_ts`.

        A single page covers whatever the trades on it happen to span, which
        is not much: measured on 4 Nov 2026 the feed ran at ~1,150 trades a
        minute, so even a full 1,000-row page is about 52 seconds of it. That
        is ample when the poller wakes every 20 seconds and close to useless
        under a scheduler that wakes it every few hours -- everything that
        traded in between is never looked at, so the bot is not rejecting
        those whales, it never sees them. Walking back to the last trade
        already processed makes coverage depend on what we ask for rather
        than on how often the process happens to start.

        Returns (trades, reached_floor). reached_floor=False means the page
        budget ran out first, which at this feed rate is the normal case for
        a multi-hour gap, not a fault -- the caller reports how much ground
        it actually covered rather than treating it as an error.
        """
        now = time.time()
        # Two independent bounds. The lookback stops a cold start, or a
        # two-day outage like the v1 retirement, from trying to pull the
        # entire feed; the page budget keeps one cycle's request count and
        # memory finite however busy the feed happens to be.
        floor = max(float(since_ts or 0.0), now - max_lookback_seconds)
        trades: list[dict] = []
        hashes: set[str] = set()
        cursor: str | None = None
        reached = False

        for _ in range(max(1, max_pages)):
            page, next_cursor, has_more = self._trades_page(
                limit=page_size, cursor=cursor)
            if not page:
                # The feed ended before our floor did; nothing older exists.
                reached = True
                break
            fresh = 0
            for t in page:
                h = t.get("transactionHash")
                if h is not None:
                    if h in hashes:
                        continue
                    hashes.add(h)
                trades.append(t)
                fresh += 1

            stamps = [parse_timestamp(t.get("timestamp")) for t in page]
            oldest = min((v for v in stamps if v is not None), default=None)
            if oldest is None or oldest <= floor:
                reached = True
                break
            if not has_more or not next_cursor:
                # v2 says this was the end of the feed, so there is nothing
                # left to miss even though we never reached the floor.
                reached = True
                break
            if not fresh or next_cursor == cursor:
                # A page we have already seen, or a cursor that did not move,
                # means paging has stopped making progress. Spending the rest
                # of the budget to rediscover that buys nothing.
                break
            cursor = next_cursor

        return trades, reached

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
