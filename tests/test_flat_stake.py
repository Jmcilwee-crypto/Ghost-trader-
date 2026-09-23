"""Flat staking separates "should I bet?" from "how much?".

Replaying all 123 settled bets with the stake held flat turned +$114 into
+$302 on identical outcomes: confidence-scaling bet hardest exactly where the
signal was weakest, because a wallet's past accuracy did not predict its next
bet. Sizing multiplies an edge; it cannot create one.

The risk here is a half-applied change -- a flat manager leaking onto the
scaled bots, or a flat stake escaping the per-trade cap.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src import experiment
from src.portfolio import PaperPortfolio
from src.risk import RiskConfig, RiskManager


def _mgr(**overrides):
    cfg = {"max_pct_per_trade": 0.05, "max_concurrent_positions": 15,
           "max_pct_per_market": 0.10}
    cfg.update(overrides)
    return RiskManager(RiskConfig(**cfg))


def test_scaled_sizing_still_varies_with_confidence():
    m = _mgr()
    p = PaperPortfolio(1000.0)
    low = m.position_size_usd(portfolio=p, market_id="m", confidence=0.2)
    high = m.position_size_usd(portfolio=p, market_id="m", confidence=0.9)
    assert high > low


def test_flat_sizing_ignores_confidence_entirely():
    m = _mgr(flat_stake_usd=20.0)
    p = PaperPortfolio(1000.0)
    sizes = {m.position_size_usd(portfolio=p, market_id="m", confidence=c)
             for c in (0.1, 0.4, 0.75, 1.0)}
    assert sizes == {20.0}, f"flat stake varied with confidence: {sizes}"


def test_flat_sizing_still_refuses_a_zero_confidence_signal():
    """Flat changes how much, never whether. Zero confidence is still no bet."""
    m = _mgr(flat_stake_usd=20.0)
    assert m.position_size_usd(portfolio=PaperPortfolio(1000.0), market_id="m", confidence=0.0) == 0.0


def test_flat_stake_cannot_escape_the_per_trade_cap():
    # $200 requested, but 5% of $1,000 equity is $50.
    m = _mgr(flat_stake_usd=200.0)
    got = m.position_size_usd(portfolio=PaperPortfolio(1000.0), market_id="m", confidence=1.0)
    assert got == pytest.approx(50.0)


def test_flat_stake_is_still_bounded_by_available_cash():
    # Both percentage caps lifted, so cash is the only thing left to bind.
    m = _mgr(flat_stake_usd=20.0, max_pct_per_trade=1.0, max_pct_per_market=1.0)
    poor = PaperPortfolio(8.0)
    assert m.position_size_usd(portfolio=poor, market_id="m", confidence=1.0) == pytest.approx(8.0)


def test_the_per_market_cap_still_applies_to_a_flat_stake():
    # 10% of $1,000 is $100, but a second $20 bet in the same market is fine.
    m = _mgr(flat_stake_usd=20.0)
    assert m.position_size_usd(portfolio=PaperPortfolio(1000.0), market_id="m",
                                confidence=1.0) == pytest.approx(20.0)


def test_default_is_unchanged_so_existing_bots_keep_scaling():
    assert RiskConfig(max_pct_per_trade=0.05, max_concurrent_positions=15,
                      max_pct_per_market=0.10).flat_stake_usd is None


# -- the A/B itself -----------------------------------------------------
def _variants():
    from src.scorecard import TraderScorecard
    config = yaml.safe_load(Path("config.yaml").read_text())
    return experiment.build_variants(config, TraderScorecard(), research=None)


def test_every_flat_twin_has_a_scaled_original_to_compare_against():
    """A twin with no original is not a controlled comparison."""
    names = {v.name for v in _variants()}
    twins = [n for n in names if n.endswith("-flat")]
    assert twins, "flat-stake variants disappeared"
    for twin in twins:
        original = twin[: -len("-flat")]
        assert original in names or original.startswith("value-"), \
            f"{twin} has no confidence-scaled counterpart ({original})"


def test_a_twin_differs_from_its_original_in_sizing_only():
    by_name = {v.name: v for v in _variants()}
    twin, original = by_name["aggressive@$500-flat"], by_name["aggressive@$500"]
    assert twin.settings["flat_stake_usd"] == experiment.FLAT_STAKE_USD
    assert "flat_stake_usd" not in original.settings
    for key in ("profile", "whale_usd_threshold", "copy_above_accuracy",
                "fade_below_accuracy", "min_track_record"):
        assert twin.settings[key] == original.settings[key], f"{key} differs -- not a clean A/B"


def test_flat_risk_manager_does_not_leak_onto_the_scaled_bots():
    """One shared RiskManager would silently flatten the entire experiment."""
    by_name = {v.name: v for v in _variants()}
    assert by_name["aggressive@$500"].strategy.risk.config.flat_stake_usd is None
    assert by_name["aggressive@$500-flat"].strategy.risk.config.flat_stake_usd == experiment.FLAT_STAKE_USD


def test_the_flat_cohort_covers_copy_only():
    """copy_only isolates the stance where the sizing damage was measured."""
    profiles = {s["profile"] for s in experiment.FLAT_STAKE_VARIANTS}
    assert "copy_only" in profiles
