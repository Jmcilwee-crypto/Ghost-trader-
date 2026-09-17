"""Bookmaker odds are only useful if the vig is stripped and the fixture is
matched correctly -- both fail silently and produce confident nonsense, so
they're pinned here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.research import build_research
from src.research.base import NullResearch, ProbabilityEstimate
from src.research.odds_api import OddsApiResearch, _devig, _similarity


def _event(home, away, books):
    """books: list of {outcome_name: decimal_odds}"""
    return {
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {"key": f"book{i}", "markets": [{"key": "h2h", "outcomes": [
                {"name": name, "price": price} for name, price in book.items()
            ]}]}
            for i, book in enumerate(books)
        ],
    }


def _provider(events, **kwargs):
    p = OddsApiResearch(api_key="test-key", **kwargs)
    p._events = events
    p._fetched_at = 9e18  # far future, so _refresh never makes a network call
    return p


def test_devig_normalizes_probabilities_to_sum_to_one():
    # Decimal odds of 1.5 and 2.5 imply 66.7% + 40% = 106.7% -- the 6.7% is vig.
    raw = {"A": 1 / 1.5, "B": 1 / 2.5}
    fair = _devig(raw)
    assert sum(fair.values()) == pytest.approx(1.0)
    assert fair["A"] == pytest.approx(0.625, abs=1e-3)
    assert fair["A"] < 1 / 1.5  # de-vigged is always below the raw implied


def test_devig_handles_empty_input():
    assert _devig({}) == {}


def test_similarity_ignores_club_name_noise():
    assert _similarity("FC Union Berlin", "Union Berlin") == 1.0
    assert _similarity("Houston Astros", "Philadelphia Phillies") == 0.0


def test_estimate_returns_devigged_consensus_probability():
    events = [_event("Philadelphia Phillies", "Houston Astros", [
        {"Houston Astros": 1.5, "Philadelphia Phillies": 2.5},
        {"Houston Astros": 1.55, "Philadelphia Phillies": 2.4},
    ])]
    provider = _provider(events)
    est = provider.estimate("Houston Astros vs. Philadelphia Phillies", "Houston Astros")

    assert est is not None
    assert 0.60 < est.probability < 0.65   # ~62.5%, i.e. below the raw 66.7%
    assert "2 books" in est.source


def test_estimate_matches_regardless_of_home_away_order():
    events = [_event("Houston Astros", "Philadelphia Phillies", [
        {"Houston Astros": 1.5, "Philadelphia Phillies": 2.5},
        {"Houston Astros": 1.5, "Philadelphia Phillies": 2.5},
    ])]
    provider = _provider(events)
    assert provider.estimate("Philadelphia Phillies vs. Houston Astros", "Houston Astros") is not None


def test_estimate_skips_markets_head_to_head_odds_cannot_price():
    events = [_event("FC Schalke 04", "1. FC Union Berlin", [
        {"FC Schalke 04": 2.0, "1. FC Union Berlin": 2.0},
        {"FC Schalke 04": 2.0, "1. FC Union Berlin": 2.0},
    ])]
    provider = _provider(events)
    # A totals line is a different question entirely -- must not be answered
    # with who-wins odds.
    assert provider.estimate("1. FC Union Berlin vs. FC Schalke 04: O/U 3.5", "Over") is None
    assert provider.estimate("Spread: Rams (-9.5)", "49ers") is None


def test_estimate_returns_none_for_unknown_fixture():
    events = [_event("Team A", "Team B", [{"Team A": 2.0, "Team B": 2.0}, {"Team A": 2.0, "Team B": 2.0}])]
    provider = _provider(events)
    assert provider.estimate("Totally Different vs. Other Team", "Totally Different") is None


def test_estimate_requires_a_minimum_number_of_books():
    events = [_event("Team A", "Team B", [{"Team A": 1.5, "Team B": 2.5}])]
    provider = _provider(events, min_books=2)
    assert provider.estimate("Team A vs. Team B", "Team A") is None


def test_confidence_rises_with_more_books_agreeing():
    two = _provider([_event("Team A", "Team B", [{"Team A": 1.5, "Team B": 2.5}] * 2)])
    six = _provider([_event("Team A", "Team B", [{"Team A": 1.5, "Team B": 2.5}] * 6)])
    assert six.estimate("Team A vs. Team B", "Team A").confidence > \
           two.estimate("Team A vs. Team B", "Team A").confidence


def test_provider_without_api_key_is_disabled_and_returns_nothing(monkeypatch):
    # Explicit about "no key anywhere": an empty api_key falls back to the
    # environment, and any other test that loaded .env will have populated it.
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    provider = OddsApiResearch(api_key="")
    assert provider.enabled is False
    assert provider.estimate("Team A vs. Team B", "Team A") is None


def test_build_research_falls_back_to_null_when_disabled():
    assert isinstance(build_research({"research": {"enabled": False}}), NullResearch)
    assert isinstance(build_research({}), NullResearch)


def test_build_research_returns_null_when_key_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    # Point at an env file that doesn't exist, so the real .env can't supply a key.
    config = {"research": {"enabled": True, "env_file": str(tmp_path / "absent.env")}}
    assert isinstance(build_research(config), NullResearch)


def test_env_file_supplies_the_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment line\nODDS_API_KEY=from-env-file\n")

    provider = build_research({"research": {"enabled": True, "env_file": str(env_file)}})
    assert provider.enabled
    assert provider.api_key == "from-env-file"


def test_real_environment_variable_beats_the_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDS_API_KEY", "from-real-env")
    env_file = tmp_path / ".env"
    env_file.write_text("ODDS_API_KEY=from-env-file\n")

    provider = build_research({"research": {"enabled": True, "env_file": str(env_file)}})
    assert provider.api_key == "from-real-env"


def test_probability_estimate_clamps_out_of_range_values():
    assert ProbabilityEstimate(probability=1.4, confidence=2.0, source="x").probability == 1.0
    assert ProbabilityEstimate(probability=-0.2, confidence=-1.0, source="x").confidence == 0.0


def test_network_failure_degrades_quietly(monkeypatch):
    import requests

    provider = OddsApiResearch(api_key="test-key")

    def boom(*args, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", boom)
    # Must not raise -- this runs inside the bot's hot loop on a flaky network.
    assert provider.estimate("Team A vs. Team B", "Team A") is None
    assert "network down" in (provider.stats()["last_error"] or "")


def test_quota_cap_stops_further_requests(monkeypatch):
    import requests

    calls = {"n": 0}

    class Resp:
        status_code = 200
        def json(self): return []

    def counted(*args, **kwargs):
        calls["n"] += 1
        return Resp()

    monkeypatch.setattr(requests, "get", counted)
    provider = OddsApiResearch(api_key="test-key", max_requests_per_day=2, cache_ttl_seconds=0)
    for _ in range(6):
        provider.estimate("Team A vs. Team B", "Team A")
    assert calls["n"] == 2  # hard stop, so the free monthly quota survives
