import os
from pathlib import Path

from .base import NullResearch, ProbabilityEstimate
from .composite import CompositeResearch
from .manifold import ManifoldResearch
from .odds_api import OddsApiResearch


def load_env_file(path: str = ".env") -> None:
    """Read KEY=value lines from a gitignored .env into the environment so the
    API key survives restarts without ever living in config.yaml. Real
    environment variables win, so an explicit `export` still overrides it."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_research(config: dict):
    """Returns a research provider, or a no-op one when it isn't configured.

    Bookmaker odds need ODDS_API_KEY, read from the environment rather than
    config.yaml so it never ends up committed alongside the settings.
    Manifold needs no key at all, so it can run even when the other can't.
    """
    cfg = (config.get("research") or {})
    if not cfg.get("enabled", False):
        return NullResearch()
    load_env_file(cfg.get("env_file", ".env"))

    providers = []

    odds = OddsApiResearch(
        cache_ttl_seconds=cfg.get("cache_ttl_seconds", 1800),
        regions=cfg.get("regions", "us,eu"),
        max_requests_per_day=cfg.get("max_requests_per_day", 120),
        min_books=cfg.get("min_books", 2),
        match_threshold=cfg.get("match_threshold", 0.5),
    )
    if odds.enabled:
        providers.append(odds)

    mf_cfg = cfg.get("manifold") or {}
    if mf_cfg.get("enabled", True):
        providers.append(ManifoldResearch(
            cache_ttl_seconds=mf_cfg.get("cache_ttl_seconds", 900),
            match_threshold=mf_cfg.get("match_threshold", 0.55),
            min_bettors=mf_cfg.get("min_bettors", 15),
            min_liquidity=mf_cfg.get("min_liquidity", 100.0),
            max_confidence=mf_cfg.get("max_confidence", 0.55),
        ))

    if not providers:
        return NullResearch()
    if len(providers) == 1:
        return providers[0]
    return CompositeResearch(providers)


__all__ = ["NullResearch", "OddsApiResearch", "ManifoldResearch", "CompositeResearch",
           "ProbabilityEstimate", "build_research"]
