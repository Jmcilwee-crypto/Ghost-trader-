"""Alpha Vantage's free tier answers a throttle and a bad ticker with the same
HTTP 200 and a prose message. Confusing the two silently drops a symbol for
the rest of the run, so the distinction is pinned here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests

from src.market_data import AlphaVantageData, ForexData, build_source


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _series(n=5):
    return {"Time Series (Daily)": {
        f"2026-09-{i + 1:02d}": {"1. open": "10", "2. high": "11", "3. low": "9",
                                  "4. close": str(100 + i), "5. volume": "1000"}
        for i in range(n)
    }}


def test_parses_daily_bars_oldest_first(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_series(4)))
    data = AlphaVantageData(api_key="real-key", min_request_interval=0)
    bars = data.get_bars("AAPL")

    assert len(bars) == 4
    assert [b.close for b in bars] == [100, 101, 102, 103]   # chronological
    assert bars[0].ts < bars[-1].ts


def test_throttle_does_not_permanently_disable_a_symbol(monkeypatch):
    throttle = {"Information": "Thank you for using Alpha Vantage! Please consider "
                                "spreading out your free API requests more sparingly "
                                "(1 request per minute)."}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(throttle))
    data = AlphaVantageData(api_key="real-key", min_request_interval=0)

    assert data.get_bars("NVDA") == []
    assert "NVDA" not in data.stats()["rejected_symbols"]   # must remain retryable
    assert data.stats()["throttled"] == 1

    # Once the throttle clears, the same symbol works.
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_series(3)))
    assert len(data.get_bars("NVDA")) == 3


def test_bad_ticker_is_disabled_so_it_stops_burning_quota(monkeypatch):
    calls = {"n": 0}

    def bad(*a, **k):
        calls["n"] += 1
        return _Resp({"Error Message": "Invalid API call."})

    monkeypatch.setattr(requests, "get", bad)
    data = AlphaVantageData(api_key="real-key", min_request_interval=0)

    data.get_bars("NOTATICKER")
    data.get_bars("NOTATICKER")
    data.get_bars("NOTATICKER")

    assert "NOTATICKER" in data.stats()["rejected_symbols"]
    assert calls["n"] == 1   # asked once, never again


def test_demo_key_rejection_disables_the_symbol(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(
        {"Information": "The **demo** API key is for demo purposes only."}))
    data = AlphaVantageData(api_key="demo", min_request_interval=0)
    data.get_bars("AAPL")
    assert "AAPL" in data.stats()["rejected_symbols"]


def test_requests_are_spaced_to_respect_one_per_minute(monkeypatch):
    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return _Resp(_series(3))

    monkeypatch.setattr(requests, "get", counted)
    data = AlphaVantageData(api_key="real-key", min_request_interval=65)

    data.get_bars("AAPL")     # allowed
    data.get_bars("MSFT")     # too soon -> skipped, not throttled by the API
    data.get_bars("NVDA")     # also too soon
    assert calls["n"] == 1


def test_outputsize_only_sent_with_a_real_key(monkeypatch):
    seen = {}

    def capture(url, params=None, **k):
        seen.update(params or {})
        return _Resp(_series(2))

    monkeypatch.setattr(requests, "get", capture)

    AlphaVantageData(api_key="demo", min_request_interval=0).get_bars("IBM")
    assert "outputsize" not in seen    # demo key rejects the extra param

    seen.clear()
    AlphaVantageData(api_key="real-key", min_request_interval=0).get_bars("IBM")
    assert seen.get("outputsize") == "compact"


def test_network_failure_serves_stale_cache(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_series(3)))
    data = AlphaVantageData(api_key="real-key", min_request_interval=0)
    good = data.get_bars("AAPL")
    assert len(good) == 3

    data.cache_ttl = 0        # force a refresh attempt
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    assert len(data.get_bars("AAPL")) == 3   # stale beats nothing


def test_build_source_picks_the_right_provider():
    assert isinstance(build_source({"source": "alphavantage"}), AlphaVantageData)
    assert isinstance(build_source({"source": "frankfurter"}), ForexData)


def test_forex_symbol_splitting():
    assert ForexData._split("EURUSD") == ("EUR", "USD")
    assert ForexData._split("gbp/usd") == ("GBP", "USD")
