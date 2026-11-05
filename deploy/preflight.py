"""Run this on a new server BEFORE deploying anything.

Cloud datacenter IPs are blocked by a number of these providers -- Polymarket
and Binance in particular are routinely refused from hosting ranges even
though they work fine from a home connection. Finding that out after an hour
of setup is miserable, so this checks every data source the bots depend on and
prints exactly which experiments would actually work from this machine.

    python3 deploy/preflight.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

UA = {"User-Agent": "ghost-trader-preflight/1.0"}
TRADES = "https://data-api.polymarket.com/v2/trades"

CHECKS = [
    ("Polymarket trades", "https://data-api.polymarket.com/v2/trades?limit=1", "Polymarket experiment", ()),
    ("Polymarket markets", "https://gamma-api.polymarket.com/markets?limit=1", "Polymarket experiment", ()),
    ("Binance klines", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1", "Crypto + Commodities", ()),
    ("Coinbase candles", "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600", "Crypto fallback", ()),
    ("Frankfurter FX", "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD", "Forex experiment", ()),
    ("Alpha Vantage", "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&apikey=demo", "Stocks experiment", ()),
    # 401/403 here still proves the host is reachable -- it just means no key
    # was sent, which is fine for a connectivity check.
    ("The Odds API", "https://api.the-odds-api.com/v4/sports/", "Value bots (research)", (401, 403)),
]


def check(url: str, also_ok: tuple = ()) -> tuple[bool, str]:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code == 200:
            return True, "ok"
        if r.status_code in also_ok:
            return True, f"reachable (HTTP {r.status_code}, no key sent)"
        return False, f"HTTP {r.status_code}"
    except requests.RequestException as exc:
        return False, type(exc).__name__


def check_trade_feed_paging() -> bool:
    """Does the feed still answer in the shape we parse, and does the cursor
    actually advance?

    v1 was retired on 2 Nov 2026 and the fleet sat dead for two days, because
    a failing cycle is indistinguishable from a quiet market from the outside.
    This asserts the contract the experiment depends on -- the v2 envelope,
    the snake_case field names, and a cursor that moves -- on the machine that
    does the trading, every run, so the next such change is visible the same
    day instead of two days later.
    """
    try:
        a = requests.get(f"{TRADES}?limit=100&takerOnly=true", headers=UA, timeout=15).json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  SKIP  trade feed: {type(exc).__name__}")
        return False
    if not isinstance(a, dict) or "data" not in a:
        print(f"  FAIL  trade feed: expected a {{data, pagination}} envelope, got "
              f"{type(a).__name__} {str(a)[:80]}")
        return False

    rows = a.get("data") or []
    required = ("transaction_hash", "proxy_wallet", "condition_id", "token_id",
                "timestamp", "price", "size")
    missing = [f for f in required if rows and f not in rows[0]]
    if not rows or missing:
        print(f"  FAIL  trade feed: fields missing from the response: {missing or 'no rows'}")
        return False

    stamps = [float(r["timestamp"]) for r in rows if r.get("timestamp")]
    span = (max(stamps) - min(stamps)) / 60.0 if len(stamps) > 1 else 0.0
    rate = (len(rows) / span) if span > 0 else 0.0
    lag = time.time() - max(stamps) if stamps else 0.0
    print(f"  INFO  feed runs ~{rate:,.0f} trades/min and is {lag:.0f}s behind now")

    cursor = (a.get("pagination") or {}).get("next_cursor")
    if not cursor:
        print("  FAIL  no next_cursor -- the walk cannot go back beyond one page.")
        return False
    try:
        b = requests.get(f"{TRADES}?limit=100&cursor={cursor}", headers=UA, timeout=15).json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  FAIL  cursor page: {type(exc).__name__}")
        return False
    ha = {r.get("transaction_hash") for r in rows}
    hb = {r.get("transaction_hash") for r in (b.get("data") or [])}
    if not hb - ha:
        print("  FAIL  the cursor does not advance -- coverage is one page per run.")
        return False
    print(f"  PASS  cursor advances ({len(hb - ha)}/100 new on page 2)")
    return True


def main() -> int:
    print("Ghost Trader AI preflight -- checking every data source from this machine\n")
    width = max(len(name) for name, *_ in CHECKS)
    blocked = []
    for name, url, powers, also_ok in CHECKS:
        ok, detail = check(url, also_ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail:<22} {powers}")
        if not ok:
            blocked.append((name, powers))

    print()
    check_trade_feed_paging()

    print()
    if not blocked:
        print("All sources reachable -- every experiment will run here.")
        return 0

    print("Blocked from this machine:")
    for name, powers in blocked:
        print(f"  - {name}  ->  {powers} will not work")
    print()
    print("If Polymarket or Binance are blocked, that is almost certainly this")
    print("server's IP being in a hosting range rather than anything misconfigured.")
    print("Options: pick a different region/provider, or keep those experiments")
    print("on a home machine and host only the ones that pass.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
