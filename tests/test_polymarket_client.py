"""Gamma excludes closed markets from the default query. Missing that meant
resolutions were never detected and every position hung open forever, so the
fallback lookup is pinned here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import polymarket_client
from src.polymarket_client import PolymarketClient


def _fake_get(responses, calls):
    """responses: list of payloads returned in order."""
    def _get(url, params=None, **kwargs):
        calls.append(dict(params or {}))
        return responses[min(len(calls) - 1, len(responses) - 1)]
    return _get


def test_open_market_found_in_one_request(monkeypatch):
    calls = []
    monkeypatch.setattr(polymarket_client, "_get", _fake_get([[{"question": "still open", "closed": False}]], calls))

    market = PolymarketClient().get_market_by_condition_id("0xabc")

    assert market["question"] == "still open"
    assert len(calls) == 1                      # no wasted second call
    assert "closed" not in calls[0]


def test_closed_market_found_via_the_fallback(monkeypatch):
    calls = []
    # Gamma's default query hides settled markets -> empty, then found.
    monkeypatch.setattr(polymarket_client, "_get",
                        _fake_get([[], [{"question": "settled", "closed": True,
                                          "outcomePrices": ["0", "1"]}]], calls))

    market = PolymarketClient().get_market_by_condition_id("0xabc")

    assert market["closed"] is True
    assert len(calls) == 2
    assert calls[1]["closed"] == "true"         # the retry that actually finds it


def test_unknown_market_returns_none_after_both_attempts(monkeypatch):
    calls = []
    monkeypatch.setattr(polymarket_client, "_get", _fake_get([[], []], calls))

    assert PolymarketClient().get_market_by_condition_id("0xnope") is None
    assert len(calls) == 2
