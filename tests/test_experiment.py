import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.experiment import (EDGE_VARIANTS, FLAT_STAKE_VARIANTS, PROFILES, WHALE_THRESHOLDS,
                            build_variants, leaderboard)

# Derived, never hardcoded: adding a cohort should not require editing a
# literal in five tests, and a stale literal hides what actually changed.
WHALE_COUNT = len(PROFILES) * len(WHALE_THRESHOLDS) + len(FLAT_STAKE_VARIANTS)
from src.scorecard import TraderScorecard

CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())


def test_builds_one_variant_per_profile_threshold_combination():
    variants = build_variants(CONFIG, TraderScorecard())
    assert len(variants) == WHALE_COUNT
    assert len({v.name for v in variants}) == WHALE_COUNT  # names are unique


def test_each_variant_gets_its_own_portfolio():
    variants = build_variants(CONFIG, TraderScorecard())
    variants[0].portfolio.cash = 123.0
    assert variants[1].portfolio.cash == CONFIG["bankroll"]["starting_usd"]


def test_variants_apply_their_own_thresholds_and_profile_settings():
    variants = {v.name: v for v in build_variants(CONFIG, TraderScorecard())}
    aggressive = variants["aggressive@$500"]
    strict = variants["strict@$10000"]

    assert aggressive.strategy.whale_usd_threshold == 500.0
    assert aggressive.strategy.min_track_record == 3
    assert strict.strategy.whale_usd_threshold == 10000.0
    assert strict.strategy.min_track_record == 10
    assert strict.strategy.copy_above == 0.70


def test_copy_only_profile_can_never_fade():
    variants = {v.name: v for v in build_variants(CONFIG, TraderScorecard())}
    copy_only = variants["copy_only@$1000"]
    # A wallet that has been wrong every single time still must not trigger a
    # fade, since accuracy can never be <= -1.
    assert copy_only.strategy.fade_below == -1.0


def test_all_variants_share_one_scorecard():
    scorecard = TraderScorecard()
    variants = build_variants(CONFIG, scorecard)
    assert all(v.strategy.scorecard is scorecard for v in variants)


def _with_value_enabled():
    """config.yaml has the value bots retired; these tests are about the
    wiring that builds them, so they turn the flag back on explicitly."""
    import copy as _c
    cfg = _c.deepcopy(CONFIG)
    cfg["edge_strategy"] = {**cfg.get("edge_strategy", {}), "enabled": True}
    return cfg


class _FakeResearch:
    enabled = True
    name = "fake"

    def estimate(self, title, outcome):
        return None


def test_no_value_variants_without_research():
    variants = build_variants(CONFIG, TraderScorecard(), research=None)
    assert len(variants) == WHALE_COUNT
    assert not any(v.settings.get("profile") == "value" for v in variants)


def test_value_variants_added_when_research_is_enabled():
    variants = build_variants(_with_value_enabled(), TraderScorecard(), research=_FakeResearch())
    value = [v for v in variants if v.settings.get("profile") == "value"]

    # +1 for the flat-stake value twin.
    assert len(variants) == WHALE_COUNT + len(EDGE_VARIANTS) + 1
    assert len(value) == len(EDGE_VARIANTS) + 1
    # The whale bots must be untouched, so the comparison stays honest.
    assert len([v for v in variants if v.settings.get("profile") != "value"]) == WHALE_COUNT
    assert {v.name for v in value} == {spec["name"] for spec in EDGE_VARIANTS} | {"value-edge10-flat"}


def test_value_variants_carry_their_own_edge_settings():
    variants = {v.name: v for v in build_variants(_with_value_enabled(), TraderScorecard(), research=_FakeResearch())}
    assert variants["value-edge5"].strategy.min_edge == 0.05
    assert variants["value-edge15"].strategy.min_edge == 0.15
    assert variants["value-edge10-bold"].strategy.kelly_fraction == 0.50


def test_leaderboard_sorts_by_equity_descending():
    variants = build_variants(CONFIG, TraderScorecard())
    variants[3].portfolio.cash = 5000.0
    variants[7].portfolio.cash = 2500.0
    rows = leaderboard(variants)
    assert rows[0]["name"] == variants[3].name
    assert rows[1]["name"] == variants[7].name
    assert rows[0]["return_pct"] > rows[1]["return_pct"]


def test_value_bots_can_be_retired_without_disabling_research():
    # Research still runs (the odds feed is shared), but no value variants are
    # built. Flipping enabled back to true must bring them straight back.
    import copy as _copy
    cfg = _copy.deepcopy(CONFIG)
    cfg["edge_strategy"] = {**cfg.get("edge_strategy", {}), "enabled": False}
    retired = build_variants(cfg, TraderScorecard(), research=_FakeResearch())
    assert not any(v.settings.get("profile") == "value" for v in retired)
    # The flat-stake value twin obeys the same gate, so only whale bots remain.
    assert len(retired) == WHALE_COUNT

    cfg["edge_strategy"]["enabled"] = True
    revived = build_variants(cfg, TraderScorecard(), research=_FakeResearch())
    # +1: the flat-stake value twin comes back with them.
    assert len([v for v in revived if v.settings.get("profile") == "value"]) == len(EDGE_VARIANTS) + 1
