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
TRADES = "https://data-api.polymarket.com/trades"

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


def check_trade_feed_paging() -> bool:
    """Does /trades honour `offset`, and how much time does one page span?

    The experiment pages the feed backwards to cover the whole gap since its
    last run, which only works if offset shifts the window. That cannot be
    tested from a network that blocks Polymarket, so it is verified here --
    on the machine that actually does the trading -- and printed every run.
    """
    try:
        a = requests.get(f"{TRADES}?limit=100&takerOnly=true", headers=UA, timeout=15).json()
        b = requests.get(f"{TRADES}?limit=100&takerOnly=true&offset=100", headers=UA, timeout=15).json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  SKIP  trade feed paging: {type(exc).__name__}")
        return False
    if not isinstance(a, list) or not isinstance(b, list) or not a:
        print("  FAIL  trade feed paging: unexpected response shape")
        return False

    ha = {t.get("transactionHash") for t in a}
    hb = {t.get("transactionHash") for t in b}
    stamps = [float(t["timestamp"]) for t in a if t.get("timestamp")]
    span = (max(stamps) - min(stamps)) / 60.0 if len(stamps) > 1 else 0.0
    print(f"  INFO  one 100-trade page spans {span:.1f} minutes of feed")

    if not hb or hb <= ha:
        print("  FAIL  offset is ignored -- paging cannot cover a scheduling gap.")
        print("        The bot will warn and fall back to one page per run.")
        return False
    pages_per_hour = 60.0 / span if span > 0 else float("inf")
    print(f"  PASS  offset works ({len(hb - ha)}/100 new on page 2) -- "
          f"~{pages_per_hour:.0f} pages per hour of gap at this page size")
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
