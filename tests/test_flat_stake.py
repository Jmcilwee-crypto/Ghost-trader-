"""Flat staking separates "should I bet?" from "how much?".

Replaying every settled bet with the stake held flat turned +$114 into +$302
on identical outcomes: confidence-scaling bet hardest exactly where the signal
was weakest, because a wallet's past accuracy did not predict its next bet.

The first live test of this was confounded -- the flat twins were added 12.5
days after the bots they were measured against, so the two halves never chose
between the same opportunities and the comparison said nothing. These tests
pin the properties that make the rebuilt cohort an actual experiment: both
arms born together, differing in exactly one setting, and no leakage of the
flat rule onto the rest of the fleet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src import experiment
from src.portfolio import PaperPortfolio
from src.risk import RiskConfig, RiskManager
from src.scorecard import TraderScorecard


def _mgr(**overrides):
    cfg = {"max_pct_per_trade": 0.05, "max_concurrent_positions": 15,
           "max_pct_per_market": 0.10}
    cfg.update(overrides)
    return RiskManager(RiskConfig(**cfg))


# -- the sizing rule itself --------------------------------------------
def test_scaled_sizing_still_varies_with_confidence():
    m, p = _mgr(), PaperPortfolio(1000.0)
    assert (m.position_size_usd(portfolio=p, market_id="m", confidence=0.9)
            > m.position_size_usd(portfolio=p, market_id="m", confidence=0.2))


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
    m = _mgr(flat_stake_usd=200.0)   # 5% of $1,000 is $50
    assert m.position_size_usd(portfolio=PaperPortfolio(1000.0), market_id="m",
                               confidence=1.0) == pytest.approx(50.0)


def test_flat_stake_is_still_bounded_by_available_cash():
    m = _mgr(flat_stake_usd=20.0, max_pct_per_trade=1.0, max_pct_per_market=1.0)
    assert m.position_size_usd(portfolio=PaperPortfolio(8.0), market_id="m",
                               confidence=1.0) == pytest.approx(8.0)


def test_default_is_unchanged_so_existing_bots_keep_scaling():
    assert RiskConfig(max_pct_per_trade=0.05, max_concurrent_positions=15,
                      max_pct_per_market=0.10).flat_stake_usd is None


# -- the cohort as an experiment ---------------------------------------
def _variants():
    config = yaml.safe_load(Path("config.yaml").read_text())
    return experiment.build_variants(config, TraderScorecard(), research=None)


def _pairs(variants):
    pairs = {}
    for v in variants:
        pid = v.settings.get("ab_pair")
        if pid:
            pairs.setdefault(pid, {})[v.settings["ab_arm"]] = v
    return pairs


def test_every_pair_has_both_arms():
    pairs = _pairs(_variants())
    assert len(pairs) == len(experiment.AB_PAIRS)
    for pid, arms in pairs.items():
        assert set(arms) == {"scaled", "flat"}, f"{pid} is missing an arm: {set(arms)}"


def test_the_two_arms_differ_in_sizing_and_nothing_else():
    """The whole validity of the experiment rests on this."""
    for pid, arms in _pairs(_variants()).items():
        a, b = arms["scaled"].settings, arms["flat"].settings
        differing = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
        assert differing == {"ab_arm", "flat_stake_usd"}, \
            f"{pid} arms differ in unexpected settings: {differing - {'ab_arm', 'flat_stake_usd'}}"


def test_both_arms_start_from_the_same_bankroll():
    """The confound that broke the first attempt: one arm had a head start."""
    for pid, arms in _pairs(_variants()).items():
        s, f = arms["scaled"].portfolio, arms["flat"].portfolio
        assert s.starting_cash == f.starting_cash
        assert s.cash == f.cash
        assert not s.positions and not f.positions
        assert not s.closed_trades and not f.closed_trades


def test_only_the_flat_arm_carries_a_flat_stake():
    for pid, arms in _pairs(_variants()).items():
        assert arms["scaled"].strategy.risk.config.flat_stake_usd is None
        assert arms["flat"].strategy.risk.config.flat_stake_usd == experiment.FLAT_STAKE_USD


def test_the_flat_rule_does_not_leak_onto_the_main_fleet():
    """One shared RiskManager would silently flatten the whole experiment."""
    for v in _variants():
        if v.settings.get("ab_arm") == "flat":
            continue
        risk = getattr(v.strategy, "risk", None)
        if risk is not None:
            assert risk.config.flat_stake_usd is None, f"{v.name} was flattened"


def test_the_cohort_covers_copy_only():
    """copy_only isolates the stance where the sizing damage was measured."""
    assert "copy_only" in {s["profile"] for s in experiment.AB_PAIRS}


def test_cohort_names_are_distinct_from_the_originals():
    """An A/B arm must never be mistaken for the long-running bot it echoes."""
    names = {v.name for v in _variants()}
    for spec in experiment.AB_PAIRS:
        original = f"{spec['profile']}@${int(spec['threshold'])}"
        assert original in names                      # untouched
        assert f"{experiment.AB_PREFIX}-{original}-scaled" in names
        assert f"{experiment.AB_PREFIX}-{original}-flat" in names
