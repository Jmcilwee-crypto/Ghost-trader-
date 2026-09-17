"""Candle data for continuous-price assets across several asset classes.

All of these feed the exact same engine -- once prices arrive as a list of
bars, a momentum bot does not care whether it's looking at Bitcoin, gold or
the euro.

  crypto       Binance (Coinbase fallback)      no key, 24/7
  commodities  Binance tokenised gold           no key, 24/7
  forex        Frankfurter (ECB reference)      no key, daily, weekdays
  stocks       Twelve Data                      needs a free key

Yahoo Finance and Stooq were tried first for equities and both refuse
programmatic access from here (HTTP 429 and a JavaScript bot-challenge
respectively), which is why stocks are the one class that needs a key.
For crypto, Binance is primary and Coinbase the fallback -- Binance blocks a
number of regions outright, and a bot that dies because it was run from the
wrong country is useless.

Every source fails soft and serves whatever is already cached rather than
raising: these sit in the hot loop of a long-running process on a connection
that has already proven unreliable.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

BINANCE = "https://api.binance.com/api/v3"
COINBASE = "https://api.exchange.coinbase.com"


@dataclass
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class CryptoData:
    """Fetches recent candles per symbol, cached for the length of one bar."""

    name = "crypto (binance/coinbase)"

    def __init__(self, *, interval: str = "1h", limit: int = 120, cache_ttl_seconds: int = 300,
                 timeout: float = 12.0):
        self.interval = interval
        self.limit = limit
        self.cache_ttl = cache_ttl_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list[Bar]]] = {}
        self._source_used: dict[str, str] = {}
        self._last_error: str | None = None
        self._requests_made = 0

    # ---- providers ----------------------------------------------------------
    def _binance(self, symbol: str) -> list[Bar]:
        resp = requests.get(
            f"{BINANCE}/klines",
            params={"symbol": symbol.replace("-", "").replace("/", ""),
                    "interval": self.interval, "limit": self.limit},
            timeout=self.timeout,
        )
        self._requests_made += 1
        resp.raise_for_status()
        rows = resp.json()
        # [openTime, open, high, low, close, volume, closeTime, ...]
        return [Bar(ts=r[0] / 1000.0, open=float(r[1]), high=float(r[2]),
                    low=float(r[3]), close=float(r[4]), volume=float(r[5])) for r in rows]

    def _coinbase(self, symbol: str) -> list[Bar]:
        granularity = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
        product = symbol if "-" in symbol else symbol.replace("USDT", "-USD")
        resp = requests.get(
            f"{COINBASE}/products/{product}/candles",
            params={"granularity": granularity.get(self.interval, 3600)},
            timeout=self.timeout,
        )
        self._requests_made += 1
        resp.raise_for_status()
        rows = resp.json()
        # [time, low, high, open, close, volume] -- newest first, so reverse.
        bars = [Bar(ts=float(r[0]), open=float(r[3]), high=float(r[2]),
                    low=float(r[1]), close=float(r[4]), volume=float(r[5])) for r in rows]
        bars.sort(key=lambda b: b.ts)
        return bars[-self.limit:]

    # ---- public -------------------------------------------------------------
    def get_bars(self, symbol: str) -> list[Bar]:
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]

        for label, fetch in (("binance", self._binance), ("coinbase", self._coinbase)):
            try:
                bars = fetch(symbol)
                if bars:
                    self._cache[symbol] = (time.time(), bars)
                    self._source_used[symbol] = label
                    self._last_error = None
                    return bars
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                self._last_error = f"{label}: {exc}"
                continue

        # Both providers failed -- serve stale data rather than dropping the
        # bot's view of the market entirely.
        return cached[1] if cached else []

    def latest_price(self, symbol: str) -> float | None:
        bars = self.get_bars(symbol)
        return bars[-1].close if bars else None

    def closes(self, symbol: str) -> list[float]:
        return [b.close for b in self.get_bars(symbol)]

    def stats(self) -> dict:
        return {
            "enabled": True,
            "symbols_cached": len(self._cache),
            "requests_made": self._requests_made,
            "sources": dict(self._source_used),
            "last_error": self._last_error,
        }


class ForexData:
    """Daily FX rates from the ECB via Frankfurter. No key, no rate limit.

    Two caveats the strategies inherit: it is reference-rate data, so there is
    one price per weekday rather than intraday bars, and it publishes close
    only -- open/high/low are filled with the close so the Bar shape matches.
    Only indicators that read closes are meaningful here.
    """

    name = "forex (ECB via Frankfurter)"
    enabled = True

    def __init__(self, *, limit: int = 150, cache_ttl_seconds: int = 3600, timeout: float = 12.0):
        self.limit = limit
        self.cache_ttl = cache_ttl_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list[Bar]]] = {}
        self._requests_made = 0
        self._last_error: str | None = None

    @staticmethod
    def _split(symbol: str) -> tuple[str, str]:
        s = symbol.replace("/", "").replace("-", "").upper()
        return s[:3], s[3:6]

    def get_bars(self, symbol: str) -> list[Bar]:
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]

        base, quote = self._split(symbol)
        # Ask for well more calendar days than bars wanted -- weekends and
        # holidays have no fixing.
        end = datetime.utcnow().date()
        start = end - timedelta(days=int(self.limit * 1.7) + 20)
        try:
            resp = requests.get(
                f"https://api.frankfurter.dev/v1/{start}..{end}",
                params={"base": base, "symbols": quote}, timeout=self.timeout,
            )
            self._requests_made += 1
            resp.raise_for_status()
            rates = resp.json().get("rates", {})
            bars = []
            for day in sorted(rates):
                value = rates[day].get(quote)
                if value is None:
                    continue
                ts = datetime.strptime(day, "%Y-%m-%d").timestamp()
                bars.append(Bar(ts=ts, open=value, high=value, low=value, close=value, volume=0.0))
            bars = bars[-self.limit:]
            if bars:
                self._cache[symbol] = (time.time(), bars)
                self._last_error = None
            return bars
        except (requests.RequestException, ValueError, KeyError) as exc:
            self._last_error = str(exc)
            return cached[1] if cached else []

    def latest_price(self, symbol: str) -> float | None:
        bars = self.get_bars(symbol)
        return bars[-1].close if bars else None

    def closes(self, symbol: str) -> list[float]:
        return [b.close for b in self.get_bars(symbol)]

    def stats(self) -> dict:
        return {"enabled": True, "symbols_cached": len(self._cache),
                "requests_made": self._requests_made, "last_error": self._last_error}


class StockData:
    """Equity candles from Twelve Data.

    This is the one class that needs a key: Yahoo Finance returns 429 to
    programmatic clients and Stooq serves a JavaScript bot-challenge, so there
    is no usable keyless equity feed. A free Twelve Data key allows 800
    requests/day, which is ample at one request per symbol per cache window.

    The key is read from TWELVEDATA_API_KEY (or the gitignored .env), never
    from config.yaml.
    """

    name = "stocks (Twelve Data)"

    def __init__(self, *, api_key: str | None = None, interval: str = "1day", limit: int = 150,
                 cache_ttl_seconds: int = 3600, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("TWELVEDATA_API_KEY")
        self.interval = interval
        self.limit = limit
        self.cache_ttl = cache_ttl_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list[Bar]]] = {}
        self._requests_made = 0
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_bars(self, symbol: str) -> list[Bar]:
        if not self.enabled:
            return []
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]
        try:
            resp = requests.get(
                "https://api.twelvedata.com/time_series",
                params={"symbol": symbol, "interval": self.interval,
                        "outputsize": self.limit, "apikey": self.api_key},
                timeout=self.timeout,
            )
            self._requests_made += 1
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") == "error":
                self._last_error = payload.get("message", "unknown error")
                return cached[1] if cached else []
            values = payload.get("values", [])
            bars = []
            for row in values:
                ts = datetime.strptime(row["datetime"][:19].replace("T", " "),
                                        "%Y-%m-%d %H:%M:%S" if len(row["datetime"]) > 10 else "%Y-%m-%d").timestamp()
                bars.append(Bar(ts=ts, open=float(row["open"]), high=float(row["high"]),
                                 low=float(row["low"]), close=float(row["close"]),
                                 volume=float(row.get("volume") or 0)))
            bars.sort(key=lambda b: b.ts)   # Twelve Data returns newest first
            if bars:
                self._cache[symbol] = (time.time(), bars)
                self._last_error = None
            return bars
        except (requests.RequestException, ValueError, KeyError) as exc:
            self._last_error = str(exc)
            return cached[1] if cached else []

    def latest_price(self, symbol: str) -> float | None:
        bars = self.get_bars(symbol)
        return bars[-1].close if bars else None

    def closes(self, symbol: str) -> list[float]:
        return [b.close for b in self.get_bars(symbol)]

    def stats(self) -> dict:
        return {"enabled": self.enabled, "symbols_cached": len(self._cache),
                "requests_made": self._requests_made, "last_error": self._last_error,
                "note": None if self.enabled else "set TWELVEDATA_API_KEY to enable"}


class AlphaVantageData:
    """Daily equity bars from Alpha Vantage.

    Works with no signup at all: their public `demo` key serves a couple of
    symbols (IBM and MSFT at time of writing), which is enough to run the
    experiment out of the box. Any other ticker returns an "Information"
    notice instead of data, which is treated as an error rather than silently
    producing an empty series.

    Set ALPHAVANTAGE_API_KEY (free, from alphavantage.co) for full coverage.
    The free tier is only ~25 requests/day, so the cache TTL here is
    deliberately hours rather than minutes -- daily bars change once a day, so
    polling harder buys nothing and would exhaust the quota before lunch.
    """

    name = "stocks (Alpha Vantage)"

    def __init__(self, *, api_key: str | None = None, limit: int = 150,
                 cache_ttl_seconds: int = 21600, timeout: float = 20.0,
                 min_request_interval: float = 65.0):
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY") or "demo"
        self.limit = limit
        self.cache_ttl = cache_ttl_seconds
        self.timeout = timeout
        # Free tier allows ONE request per minute. Asking for five symbols in a
        # tight loop gets four of them throttled, so requests are spaced and at
        # most one symbol is refreshed per cycle -- the rest keep serving cache.
        self.min_request_interval = min_request_interval
        self._last_request_at = 0.0
        self._cache: dict[str, tuple[float, list[Bar]]] = {}
        self._requests_made = 0
        self._throttled = 0
        self._last_error: str | None = None
        self._rejected: set[str] = set()

    @property
    def enabled(self) -> bool:
        return True

    @property
    def using_demo_key(self) -> bool:
        return self.api_key == "demo"

    def get_bars(self, symbol: str) -> list[Bar]:
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] < self.cache_ttl:
            return cached[1]
        if symbol in self._rejected:
            return cached[1] if cached else []
        if time.time() - self._last_request_at < self.min_request_interval:
            # Too soon after the last call: serve stale data rather than
            # spending a request that would only come back throttled.
            return cached[1] if cached else []
        self._last_request_at = time.time()

        # The demo key only answers their exact documented query -- adding
        # outputsize gets it rejected outright, so only send it with a real key.
        params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "apikey": self.api_key}
        if not self.using_demo_key:
            params["outputsize"] = "compact"
        try:
            resp = requests.get("https://www.alphavantage.co/query", params=params, timeout=self.timeout)
            self._requests_made += 1
            resp.raise_for_status()
            payload = resp.json()
            series = payload.get("Time Series (Daily)")
            if not series:
                note = str(payload.get("Information") or payload.get("Note")
                           or payload.get("Error Message") or "no data")
                self._last_error = f"{symbol}: {note[:110]}"
                lowered = note.lower()
                # A throttle is temporary and must NOT disable the ticker --
                # treating it as fatal would permanently drop a symbol just
                # because requests bunched up.
                if "sparingly" in lowered or "per minute" in lowered or "rate limit" in lowered:
                    self._throttled += 1
                elif "demo" in lowered or "Error Message" in payload:
                    # A demo-key rejection or bad ticker will never succeed on
                    # retry, so stop asking instead of burning the daily quota.
                    self._rejected.add(symbol)
                return cached[1] if cached else []

            bars = []
            for day in sorted(series):
                row = series[day]
                bars.append(Bar(
                    ts=datetime.strptime(day, "%Y-%m-%d").timestamp(),
                    open=float(row["1. open"]), high=float(row["2. high"]),
                    low=float(row["3. low"]), close=float(row["4. close"]),
                    volume=float(row.get("5. volume") or 0),
                ))
            bars = bars[-self.limit:]
            if bars:
                self._cache[symbol] = (time.time(), bars)
                self._last_error = None
            return bars
        except (requests.RequestException, ValueError, KeyError) as exc:
            self._last_error = f"{symbol}: {exc}"
            return cached[1] if cached else []

    def latest_price(self, symbol: str) -> float | None:
        bars = self.get_bars(symbol)
        return bars[-1].close if bars else None

    def closes(self, symbol: str) -> list[float]:
        return [b.close for b in self.get_bars(symbol)]

    def stats(self) -> dict:
        return {
            "enabled": True,
            "symbols_cached": len(self._cache),
            "requests_made": self._requests_made,
            "throttled": self._throttled,
            "last_error": self._last_error,
            "rejected_symbols": sorted(self._rejected),
            "note": "using shared demo key -- set ALPHAVANTAGE_API_KEY for full ticker coverage"
                     if self.using_demo_key else None,
        }


def build_source(spec: dict):
    """Pick a data source from an asset class's config block."""
    source = (spec.get("source") or "binance").lower()
    limit = spec.get("bars", 150)
    ttl = spec.get("cache_ttl_seconds", 300)
    if source == "frankfurter":
        return ForexData(limit=limit, cache_ttl_seconds=max(ttl, 1800))
    if source == "twelvedata":
        return StockData(interval=spec.get("interval", "1day"), limit=limit,
                         cache_ttl_seconds=max(ttl, 1800))
    if source in ("alphavantage", "stocks"):
        return AlphaVantageData(limit=limit, cache_ttl_seconds=max(ttl, 21600))
    return CryptoData(interval=spec.get("interval", "1h"), limit=limit, cache_ttl_seconds=ttl)


if __name__ == "__main__":
    import sys

    data = CryptoData()
    for sym in (sys.argv[1:] or ["BTCUSDT", "ETHUSDT"]):
        bars = data.get_bars(sym)
        if bars:
            print(f"{sym}: {len(bars)} bars, latest close ${bars[-1].close:,.2f} "
                  f"(via {data._source_used.get(sym)})")
        else:
            print(f"{sym}: no data ({data._last_error})")
