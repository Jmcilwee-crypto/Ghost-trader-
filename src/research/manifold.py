"""Probability estimates from Manifold Markets -- a second crowd's opinion.

Why a rival prediction market: every other signal in this project looks at
Polymarket's own price history and guesses where it goes next. Across 56 spot
bots that approach never beat doing nothing. This is different in kind. When
the same question trades at 0.62 on Polymarket and 0.71 on Manifold, two
crowds have reached different conclusions about the same future, and at least
one of them is wrong. That disagreement is a *reason* for an edge to exist,
which is more than "the line went up recently" can claim.

Three things keep this honest:

1. **Manifold is play money.** Its prices are real forecasts but the incentive
   to correct a bad one is weaker than on a real-money book, and it skews
   toward a tech-literate, US-centric userbase. So estimates here are
   deliberately capped below the confidence given to bookmaker consensus.
2. **A thin market is noise.** A question with four bettors is one person's
   opinion wearing a probability's clothes. Liquidity gates the confidence.
3. **Matching is the whole risk.** "Will Trump win in 2028?" and "Will Trump
   run in 2028?" share almost every token and mean different things, so the
   match threshold is strict and negation mismatches are rejected outright.

No API key and no account needed -- Manifold's read endpoints are public.
"""
from __future__ import annotations

import re
import time

import requests

from .base import ProbabilityEstimate

API_ROOT = "https://api.manifold.markets/v0"

# Words that flip a question's meaning. If one side has it and the other
# doesn't, a high token overlap is actively misleading rather than reassuring.
_NEGATIONS = {"not", "no", "never", "fail", "fails", "without", "lose", "loses", "against"}

# Common filler that inflates Jaccard overlap between unrelated questions.
_NOISE = {"will", "the", "a", "an", "be", "is", "in", "on", "at", "of", "to", "by",
          "for", "and", "or", "this", "that", "it", "any", "before", "after", "during"}


def _tokens(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    return {t for t in cleaned.split() if t and t not in _NOISE}


_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december",
           "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}


def _thresholds(tokens: set[str]) -> set[str]:
    """Numbers that define the question rather than date it.

    A year is context ("...in 2026"), and so is a day-of-month when a month
    name sits beside it -- "November 19, 2026" is one date, not a threshold
    of 19. Anything else is usually the thing being asked: $200,000 vs
    $150,000, 25bps vs 50bps. Two questions whose thresholds differ are
    different questions however much wording they share.
    """
    dated = bool(tokens & _MONTHS)
    out = set()
    for t in tokens:
        if not t.isdigit():
            continue
        n = int(t)
        if 1900 <= n <= 2100:          # a year
            continue
        if dated and 1 <= n <= 31:     # a day, next to a month name
            continue
        out.add(t)
    return out


def _similarity(a: str, b: str) -> float:
    """Token overlap, with two hard vetoes for the failure modes that matter.

    Plain Jaccard is too harsh on real pairs -- "Will GTA 6 be released in
    2026?" against "Will GTA 6 come out on November 19, 2026?" scores 0.38
    despite being the same event, because the extra date words all count
    against it. Containment (shared / shorter) handles that, but it makes the
    dangerous case worse: "Will Trump win in 2028?" and "Will Trump run in
    2028?" score 0.67 while meaning different things.

    So: containment for the score, and veto anything where a negation or a
    numeric threshold disagrees. Lexical matching cannot tell "win" from
    "run", so the threshold stays high and coverage stays deliberately low --
    a question left unanswered costs nothing, a confidently wrong probability
    costs a bet.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    # Opposite questions sharing every other word.
    if bool(ta & _NEGATIONS) != bool(tb & _NEGATIONS):
        return 0.0
    # "$200,000?" vs "$150,000?" -- same shape, different question.
    if _thresholds(ta) != _thresholds(tb):
        return 0.0
    overlap = len(ta & tb)
    if overlap < 2:              # one shared word is a coincidence
        return 0.0
    # Containment, read as "does the other question ask everything mine asks?"
    # This is what separates the two 0.5-Jaccard cases: the Fed pair covers
    # every word of the shorter question (1.0) while Trump win/run leaves one
    # uncovered (0.67), and that uncovered word is exactly the verb that makes
    # them different questions. The threshold is set just under 1.0 for that
    # reason -- near-total coverage or nothing.
    return overlap / min(len(ta), len(tb))


class ManifoldResearch:
    """Looks up a Polymarket question on Manifold and reports its price."""

    name = "manifold"
    enabled = True

    def __init__(self, *, cache_ttl_seconds: int = 900, match_threshold: float = 0.85,
                 min_bettors: int = 15, min_liquidity: float = 100.0,
                 max_confidence: float = 0.55, timeout: int = 12,
                 min_request_interval: float = 1.0):
        self.cache_ttl = cache_ttl_seconds
        self.match_threshold = match_threshold
        self.min_bettors = min_bettors
        self.min_liquidity = min_liquidity
        self.max_confidence = max_confidence
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self._cache: dict[str, tuple[float, list]] = {}
        self._last_request_at = 0.0
        self._requests = 0
        self._matched = 0
        self._unmatched = 0
        self._last_error: str | None = None

    # -- fetching --------------------------------------------------------
    def _search(self, term: str) -> list:
        """Search Manifold, cached. Returns [] on any failure -- this runs in
        the bot's hot loop and must never raise."""
        key = term.lower().strip()[:120]
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]

        # Be a polite guest on a free public API.
        wait = self.min_request_interval - (now - self._last_request_at)
        if wait > 0:
            if cached:
                return cached[1]
            time.sleep(min(wait, self.min_request_interval))

        try:
            self._last_request_at = time.time()
            self._requests += 1
            resp = requests.get(
                f"{API_ROOT}/search-markets",
                params={"term": term[:120], "limit": 12, "sort": "score"},
                timeout=self.timeout,
                headers={"User-Agent": "ghost-trader-ai/1.0 (paper trading research)"},
            )
            if resp.status_code != 200:
                self._last_error = f"HTTP {resp.status_code}"
                return []
            data = resp.json()
            if not isinstance(data, list):
                self._last_error = "unexpected response shape"
                return []
        except Exception as exc:
            self._last_error = str(exc)[:160]
            return []

        self._cache[key] = (time.time(), data)
        return data

    # -- matching --------------------------------------------------------
    def _best_match(self, title: str) -> tuple[dict | None, float]:
        best, best_score = None, 0.0
        for market in self._search(title):
            if not isinstance(market, dict):
                continue
            if market.get("outcomeType") != "BINARY" or market.get("isResolved"):
                continue
            if market.get("probability") is None:
                continue
            score = _similarity(title, market.get("question", ""))
            if score > best_score:
                best, best_score = market, score
        return (best, best_score) if best_score >= self.match_threshold else (None, best_score)

    def _confidence(self, market: dict, score: float) -> float:
        """Liquidity and match quality both gate how much this is worth.

        Deliberately capped: play-money prices are informative but not as
        sharp as a bookmaker's line, and the strategy should never weight
        them as if they were.
        """
        bettors = market.get("uniqueBettorCount") or 0
        liquidity = market.get("totalLiquidity") or 0.0
        depth = min(1.0, bettors / 100.0) * 0.6 + min(1.0, liquidity / 2000.0) * 0.4
        return max(0.0, min(self.max_confidence, depth * score))

    # -- the interface the strategy calls --------------------------------
    def estimate(self, title: str, outcome: str) -> ProbabilityEstimate | None:
        market, score = self._best_match(title)
        if market is None:
            self._unmatched += 1
            return None

        bettors = market.get("uniqueBettorCount") or 0
        liquidity = market.get("totalLiquidity") or 0.0
        if bettors < self.min_bettors or liquidity < self.min_liquidity:
            self._unmatched += 1
            return None

        prob = float(market["probability"])
        # Manifold prices the YES side of its own question. Only a Yes/No
        # outcome maps onto that cleanly -- anything else (a candidate name,
        # a bracket) is a different question wearing similar words.
        label = (outcome or "").strip().lower()
        if label in ("yes", "y", "true"):
            pass
        elif label in ("no", "n", "false"):
            prob = 1.0 - prob
        else:
            self._unmatched += 1
            return None

        self._matched += 1
        return ProbabilityEstimate(
            probability=prob,
            confidence=self._confidence(market, score),
            source=f"Manifold ({bettors} bettors)",
            detail=f'matched "{market.get("question", "")[:90]}" at {score:.0%} similarity',
        )

    def stats(self) -> dict:
        return {
            "enabled": True,
            "provider": self.name,
            "requests_made": self._requests,
            "matched": self._matched,
            "unmatched": self._unmatched,
            "cached_terms": len(self._cache),
            "last_error": self._last_error,
        }
