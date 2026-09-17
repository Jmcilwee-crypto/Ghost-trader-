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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

UA = {"User-Agent": "ghost-trader-preflight/1.0"}

CHECKS = [
    ("Polymarket trades", "https://data-api.polymarket.com/trades?limit=1", "Polymarket experiment", ()),
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


def main() -> int:
    print("Ghost Trader preflight -- checking every data source from this machine\n")
    width = max(len(name) for name, *_ in CHECKS)
    blocked = []
    for name, url, powers, also_ok in CHECKS:
        ok, detail = check(url, also_ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail:<22} {powers}")
        if not ok:
            blocked.append((name, powers))

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
