"""Manifold is a second crowd pricing the same questions. The danger is not
a bad price -- it's a confident answer to the wrong question. Two questions
can share almost every word and mean opposite things, and a play-money market
with four bettors is one person's opinion wearing a probability's clothes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests

from src.research import build_research
from src.research.base import NullResearch
from src.research.composite import CompositeResearch
from src.research.manifold import ManifoldResearch, _similarity


def _market(question, prob=0.6, bettors=200, liquidity=5000.0, resolved=False, kind="BINARY"):
    return {"question": question, "probability": prob, "uniqueBettorCount": bettors,
            "totalLiquidity": liquidity, "isResolved": resolved, "outcomeType": kind}


def _provider(markets, **kwargs):
    p = ManifoldResearch(**kwargs)
    p._search = lambda term: markets          # no network in tests
    return p


# -- matching ----------------------------------------------------------
def test_negation_on_one_side_only_never_matches():
    """'Will X happen' vs 'Will X not happen' share every meaningful token."""
    assert _similarity("Will Bitcoin reach 200k?", "Will Bitcoin not reach 200k?") == 0.0


def test_similar_questions_still_match_through_filler():
    assert _similarity("Will the Fed cut rates in March?", "Will Fed cut rates March") > 0.8


def test_unrelated_questions_do_not_match():
    assert _similarity("Will it rain in Paris?", "Who wins the Champions League?") < 0.2


MATCH_THRESHOLD = 0.85


@pytest.mark.parametrize("a,b,should_match", [
    # The pair this is built for: extra qualifying words, same question.
    ("Will the Fed cut rates in 2026?",
     "Will Fed (Kevin Warsh) cut interest rates by July 2026?", True),
    ("Will GTA 6 release on November 19, 2026?",
     "Will GTA 6 release on November 19, 2026?", True),
    # One uncovered verb is the whole difference in meaning. Jaccard scores
    # this identically to the Fed pair above, which is why containment is used.
    ("Will Trump win in 2028?", "Will Trump run in 2028?", False),
    # Same shape, different threshold.
    ("Will Bitcoin reach $200000 in 2026?", "Will Bitcoin reach $150000 in 2026?", False),
    ("Will the Fed cut by 25 bps?", "Will the Fed cut by 50 bps?", False),
    # Negation.
    ("Will Bitcoin reach 200000?", "Will Bitcoin not reach 200000?", False),
    # Unrelated but both about a well-known thing.
    ("Will WW3 happen before GTA6?", "Will GTA 6 be released in 2026?", False),
])
def test_matcher_on_the_cases_that_actually_bite(a, b, should_match):
    assert (_similarity(a, b) >= MATCH_THRESHOLD) is should_match


def test_a_day_of_month_is_not_treated_as_a_threshold():
    """'November 19' is one date; without this the 19 vetoes the match."""
    assert _similarity("Will GTA 6 release in November 2026?",
                       "Will GTA 6 release on November 19, 2026?") >= MATCH_THRESHOLD


def test_a_single_shared_word_is_never_a_match():
    assert _similarity("Will Bitcoin crash?", "Will Ethereum flip Bitcoin?") == 0.0


def test_matched_market_returns_its_probability():
    p = _provider([_market("Will the Fed cut rates in March?", prob=0.72)])
    est = p.estimate("Will the Fed cut rates in March?", "Yes")
    assert est is not None
    assert est.probability == pytest.approx(0.72)
    assert "Manifold" in est.source


def test_no_outcome_inverts_the_probability():
    p = _provider([_market("Will the Fed cut rates in March?", prob=0.72)])
    assert p.estimate("Will the Fed cut rates in March?", "No").probability == pytest.approx(0.28)


def test_a_named_outcome_is_refused_rather_than_guessed():
    """Manifold prices YES on its own question. Mapping a candidate name onto
    that is a guess, and a confident wrong probability is worse than none."""
    p = _provider([_market("Who will win the 2028 election?", prob=0.4)])
    assert p.estimate("Who will win the 2028 election?", "Gavin Newsom") is None


def test_a_poor_title_match_returns_nothing():
    p = _provider([_market("Will Arsenal win the Premier League?")])
    assert p.estimate("Will inflation exceed 4% in June?", "Yes") is None


# -- quality gates ------------------------------------------------------
def test_thin_markets_are_rejected():
    assert _provider([_market("Will X happen?", bettors=4)]).estimate("Will X happen?", "Yes") is None
    assert _provider([_market("Will X happen?", liquidity=10.0)]).estimate("Will X happen?", "Yes") is None


def test_resolved_and_non_binary_markets_are_ignored():
    assert _provider([_market("Will X happen?", resolved=True)]).estimate("Will X happen?", "Yes") is None
    assert _provider([_market("Will X happen?", kind="MULTIPLE_CHOICE")]).estimate("Will X happen?", "Yes") is None


def test_confidence_rises_with_liquidity_but_stays_capped():
    thin = _provider([_market("Will X happen?", bettors=20, liquidity=200.0)])
    deep = _provider([_market("Will X happen?", bettors=900, liquidity=50000.0)])
    a = thin.estimate("Will X happen?", "Yes").confidence
    b = deep.estimate("Will X happen?", "Yes").confidence
    assert b > a
    # Play money must never be weighted like a real-money book.
    assert b <= 0.55


def test_max_confidence_is_configurable_and_enforced():
    p = _provider([_market("Will X happen?", bettors=9999, liquidity=1e6)], max_confidence=0.2)
    assert p.estimate("Will X happen?", "Yes").confidence <= 0.2


# -- failure behaviour --------------------------------------------------
def test_network_failure_returns_none_and_never_raises(monkeypatch):
    p = ManifoldResearch()

    def boom(*a, **k):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "get", boom)
    assert p.estimate("Will X happen?", "Yes") is None
    assert "network down" in (p.stats()["last_error"] or "")


def test_a_non_200_is_swallowed(monkeypatch):
    class Resp:
        status_code = 503
        def json(self): return []
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    p = ManifoldResearch()
    assert p.estimate("Will X happen?", "Yes") is None
    assert p.stats()["last_error"] == "HTTP 503"


def test_unexpected_response_shape_is_swallowed(monkeypatch):
    class Resp:
        status_code = 200
        def json(self): return {"error": "nope"}   # dict, not the expected list
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    assert ManifoldResearch().estimate("Will X happen?", "Yes") is None


# -- composite ----------------------------------------------------------
class _Stub:
    enabled = True
    def __init__(self, name, est): self.name, self._est = name, est
    def estimate(self, t, o): return self._est
    def stats(self): return {}


class _Boom(_Stub):
    def estimate(self, t, o): raise RuntimeError("provider exploded")


def test_composite_prefers_the_more_confident_answer():
    from src.research.base import ProbabilityEstimate
    low = ProbabilityEstimate(probability=0.3, confidence=0.2, source="low")
    high = ProbabilityEstimate(probability=0.8, confidence=0.9, source="high")
    c = CompositeResearch([_Stub("a", low), _Stub("b", high)])
    assert c.estimate("q", "Yes").source == "high"
    # order must not matter
    assert CompositeResearch([_Stub("b", high), _Stub("a", low)]).estimate("q", "Yes").source == "high"


def test_composite_survives_one_provider_throwing():
    from src.research.base import ProbabilityEstimate
    good = ProbabilityEstimate(probability=0.5, confidence=0.4, source="good")
    c = CompositeResearch([_Boom("bad", None), _Stub("good", good)])
    assert c.estimate("q", "Yes").source == "good"


def test_composite_is_disabled_when_it_has_no_providers():
    assert CompositeResearch([]).enabled is False


def test_manifold_runs_without_any_api_key(monkeypatch, tmp_path):
    """The whole point: no key, no account, so it works when odds do not."""
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    provider = build_research({"research": {
        "enabled": True, "env_file": str(tmp_path / "absent.env"),
        "manifold": {"enabled": True},
    }})
    assert not isinstance(provider, NullResearch)
    assert provider.enabled


def test_disabling_manifold_with_no_odds_key_falls_back_to_null(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    provider = build_research({"research": {
        "enabled": True, "env_file": str(tmp_path / "absent.env"),
        "manifold": {"enabled": False},
    }})
    assert isinstance(provider, NullResearch)
