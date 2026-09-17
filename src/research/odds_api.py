"""Probability estimates from sportsbook consensus odds (the-odds-api.com).

Why bookmakers: a sharp bookmaker's line is about the best-calibrated public
probability estimate that exists for a sporting event. They price thousands of
events with real money at stake and get corrected instantly when wrong. If
Polymarket says 55% and the book consensus says 68%, that gap is a far more
credible edge than anything scraped from news text.

Two things have to be handled carefully or the numbers are junk:

1. **Vig.** Book odds deliberately sum to more than 100% -- that overround is
   their margin. Raw implied probabilities are therefore all biased high, so
   they're normalised back to sum to 1 before use.
2. **Quota.** The free tier is 500 requests/month, so this fetches every
   upcoming event across all sports in ONE request and caches it, rather than
   querying per market.
"""
from __future__ import annotations

import os
import re
import time

import requests

from .base import ProbabilityEstimate

API_ROOT = "https://api.the-odds-api.com/v4"

# Market types whose title implies something other than a plain "who wins",
# where head-to-head odds simply don't answer the question being asked.
_UNSUPPORTED_TITLE_MARKERS = ("o/u", "over/under", "spread:", "halftime", "half-time",
                              "first half", "corners", "handicap", "total ")

# Noise words in club names that stop "FC Union Berlin" matching "Union Berlin".
_NOISE_TOKENS = {"fc", "sk", "cf", "sc", "afc", "cd", "ac", "as", "ss", "us", "the",
                 "club", "city", "1", "04", "05", "united"}


def _normalize(name: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    tokens = {t for t in cleaned.split() if t and t not in _NOISE_TOKENS}
    return tokens


def _similarity(a: str, b: str) -> float:
    """Jaccard overlap of the meaningful tokens in two team names."""
    ta, tb = _normalize(a), _normalize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _devig(implied: dict[str, float]) -> dict[str, float]:
    """Scale raw implied probabilities so they sum to 1, removing the book's
    margin. Without this every probability reads several points too high."""
    total = sum(implied.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in implied.items()}


class OddsApiResearch:
    """Consensus probabilities from bookmaker odds, cached and quota-aware."""

    name = "bookmaker odds"

    def __init__(self, *, api_key: str | None = None, cache_ttl_seconds: int = 1800,
                 regions: str = "us,eu", max_requests_per_day: int = 120,
                 min_books: int = 2, match_threshold: float = 0.5):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        self.cache_ttl = cache_ttl_seconds
        self.regions = regions
        self.max_requests_per_day = max_requests_per_day
        self.min_books = min_books
        self.match_threshold = match_threshold

        self._events: list[dict] = []
        self._fetched_at = 0.0
        self._requests_made = 0
        self._window_started = time.time()
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ---- fetching -----------------------------------------------------------
    def _budget_left(self) -> bool:
        if time.time() - self._window_started > 86400:
            self._requests_made = 0
            self._window_started = time.time()
        return self._requests_made < self.max_requests_per_day

    def _refresh(self) -> None:
        """One request covers every upcoming event across all sports, which is
        what keeps this inside the free tier."""
        if not self.enabled or not self._budget_left():
            return
        if time.time() - self._fetched_at < self.cache_ttl:
            return
        try:
            resp = requests.get(
                f"{API_ROOT}/sports/upcoming/odds",
                params={"apiKey": self.api_key, "regions": self.regions,
                        "markets": "h2h", "oddsFormat": "decimal"},
                timeout=10,
            )
            self._requests_made += 1
            if resp.status_code != 200:
                self._last_error = f"HTTP {resp.status_code}"
                self._fetched_at = time.time()  # don't hammer a failing endpoint
                return
            data = resp.json()
            if isinstance(data, list):
                self._events = data
                self._last_error = None
            self._fetched_at = time.time()
        except (requests.RequestException, ValueError) as exc:
            # Fail soft: the bot keeps running on whatever is already cached.
            self._last_error = str(exc)
            self._fetched_at = time.time()

    # ---- matching -----------------------------------------------------------
    def _find_event(self, title: str) -> dict | None:
        """Polymarket titles look like 'Houston Astros vs. Philadelphia
        Phillies'; book events carry home_team/away_team separately."""
        parts = re.split(r"\s+vs\.?\s+", title, flags=re.IGNORECASE)
        if len(parts) != 2:
            return None
        left, right = parts[0], parts[1].split(":")[0]

        best, best_score = None, 0.0
        for event in self._events:
            home, away = event.get("home_team", ""), event.get("away_team", "")
            direct = (_similarity(left, home) + _similarity(right, away)) / 2
            swapped = (_similarity(left, away) + _similarity(right, home)) / 2
            score = max(direct, swapped)
            if score > best_score:
                best, best_score = event, score
        return best if best_score >= self.match_threshold else None

    def _consensus(self, event: dict) -> tuple[dict[str, float], int]:
        """Average each outcome's de-vigged probability across every book."""
        per_book: list[dict[str, float]] = []
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                implied = {}
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name = outcome.get("name")
                    if not name or not isinstance(price, (int, float)) or price <= 1:
                        continue
                    implied[name] = 1.0 / price
                if len(implied) >= 2:
                    per_book.append(_devig(implied))

        if not per_book:
            return {}, 0
        names = set().union(*[set(b) for b in per_book])
        consensus = {}
        for name in names:
            vals = [b[name] for b in per_book if name in b]
            if vals:
                consensus[name] = sum(vals) / len(vals)
        return consensus, len(per_book)

    # ---- public API ----------------------------------------------------------
    def estimate(self, title: str, outcome: str) -> ProbabilityEstimate | None:
        if not self.enabled:
            return None
        lowered = (title or "").lower()
        if any(marker in lowered for marker in _UNSUPPORTED_TITLE_MARKERS):
            return None  # head-to-head odds can't price a total or a spread

        self._refresh()
        if not self._events:
            return None

        event = self._find_event(title)
        if event is None:
            return None

        consensus, book_count = self._consensus(event)
        if not consensus or book_count < self.min_books:
            return None

        best_name, best_score = None, 0.0
        for name in consensus:
            score = _similarity(outcome, name)
            if score > best_score:
                best_name, best_score = name, score
        if best_name is None or best_score < self.match_threshold:
            return None

        # More books agreeing is a firmer number; taper off above ~6.
        confidence = min(1.0, 0.4 + 0.1 * book_count)
        return ProbabilityEstimate(
            probability=consensus[best_name],
            confidence=confidence,
            source=f"bookmaker consensus ({book_count} books)",
            detail=f"{best_name} priced at {consensus[best_name]:.1%} across {book_count} books",
        )

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "requests_made": self._requests_made,
            "events_cached": len(self._events),
            "cache_age_seconds": round(time.time() - self._fetched_at) if self._fetched_at else None,
            "last_error": self._last_error,
        }
