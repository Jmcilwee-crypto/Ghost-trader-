"""Shared types for probability research.

A research provider's job is to answer one question: given a market question
and one of its outcomes, what's the real-world probability that outcome
happens? The strategy then compares that against what Polymarket is charging
and bets only when the gap is big enough to be worth it.

Providers must fail soft. They sit in the hot path of a long-running bot on
an unreliable connection, so an unreachable API or an unparseable response
has to return None, never raise.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProbabilityEstimate:
    """An outside opinion on how likely an outcome is."""
    probability: float          # 0-1, already de-vigged where applicable
    confidence: float           # 0-1, how much weight the strategy should give it
    source: str                 # e.g. "bookmaker consensus (7 books)"
    detail: str = ""            # human-readable note for the dashboard

    def __post_init__(self):
        self.probability = max(0.0, min(1.0, self.probability))
        self.confidence = max(0.0, min(1.0, self.confidence))


class NullResearch:
    """Stands in when no provider is configured, so callers don't need to
    special-case a missing one."""

    enabled = False
    name = "none"

    def estimate(self, title: str, outcome: str) -> ProbabilityEstimate | None:
        return None

    def stats(self) -> dict:
        return {"enabled": False, "requests_made": 0}
