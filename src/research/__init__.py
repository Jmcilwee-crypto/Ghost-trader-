import os
from pathlib import Path

from .base import NullResearch, ProbabilityEstimate
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

    The API key comes from the ODDS_API_KEY environment variable rather than
    config.yaml so it never ends up committed alongside the settings.
    """
    cfg = (config.get("research") or {})
    if not cfg.get("enabled", False):
        return NullResearch()
    load_env_file(cfg.get("env_file", ".env"))
    provider = OddsApiResearch(
        cache_ttl_seconds=cfg.get("cache_ttl_seconds", 1800),
        regions=cfg.get("regions", "us,eu"),
        max_requests_per_day=cfg.get("max_requests_per_day", 120),
        min_books=cfg.get("min_books", 2),
        match_threshold=cfg.get("match_threshold", 0.5),
    )
    return provider if provider.enabled else NullResearch()


__all__ = ["NullResearch", "OddsApiResearch", "ProbabilityEstimate", "build_research"]
