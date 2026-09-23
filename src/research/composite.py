"""Combines several research providers into one.

They cover different ground rather than competing: bookmaker consensus can
price a tennis match but has nothing to say about "Will OpenAI release GPT-6
before July?", and Manifold has an opinion on the second but rarely beats a
sharp book on the first.

The most *confident* answer wins rather than the first one. Each provider
scores its own confidence on the same 0-1 scale, with play-money estimates
deliberately capped lower than real-money ones, so this resolves to "prefer
the better-evidenced opinion" rather than "prefer whichever we happened to
list first".
"""
from __future__ import annotations

from .base import ProbabilityEstimate


class CompositeResearch:
    def __init__(self, providers: list):
        self.providers = [p for p in providers if getattr(p, "enabled", False)]

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    @property
    def name(self) -> str:
        return "+".join(getattr(p, "name", "?") for p in self.providers) or "none"

    def estimate(self, title: str, outcome: str) -> ProbabilityEstimate | None:
        best: ProbabilityEstimate | None = None
        for provider in self.providers:
            try:
                est = provider.estimate(title, outcome)
            except Exception:
                # A provider that throws must not take the bot down with it;
                # the others can still answer.
                continue
            if est is not None and (best is None or est.confidence > best.confidence):
                best = est
        return best

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "provider": self.name,
            "providers": {getattr(p, "name", "?"): p.stats() for p in self.providers},
        }
