"""Two measurement guards.

Capacity: at ~27 whale trades a minute the fleet sees far more signal than its
15 position slots can hold, and the explore arm is held to a tighter sub-cap
than copy. So "explore placed fewer bets" stopped meaning "explore found
fewer opportunities" -- it can mean explore was turned away. A denial rate is
only readable if refusals are counted against the arm that wanted them.

Regimes: how much of the feed a cycle could see changed on 22 Oct (paging),
on 2 Nov (v1 retired, nothing recorded) and on 5 Nov (v2). Pooling bets
across those compares different experiments. The A/B report already refuses
to print a number for arms that didn't start together; this is that guard
applied to the fleet's own history.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import stats
from src.experiment import DENIAL_KINDS, PRE_INTENT_ACTIONS, TAKEN_ACTIONS, count_decision
from src.portfolio import PaperPortfolio
from src.strategy.base import Evaluation


def _ev(action, stance):
    return Evaluation(wallet="0x1", market_id="m", title="t", outcome="Yes", price=0.5,
                      usd_size=1000.0, timestamp=0.0, accuracy=None, track_record_count=0,
                      action=action, reason="", possibility_pct=None, importance_pct=0.0,
                      signal=None, intended_stance=stance)


# -- counting ---------------------------------------------------------------
def test_a_refusal_is_counted_against_the_arm_that_wanted_it():
    c = {}
    count_decision(c, _ev("skipped_explore_cap", "explore"))
    assert c == {"explore": {"no_slot": 1}}


def test_taken_and_denied_land_in_the_same_arm():
    c = {}
    count_decision(c, _ev("copy", "copy"))
    count_decision(c, _ev("skipped_full", "copy"))
    assert c["copy"] == {"taken": 1, "no_slot": 1}


def test_capacity_and_preference_are_not_conflated():
    """'No room' and 'looked and didn't want it' must not share a bucket --
    that conflation is what made the explore arm look merely less active."""
    c = {}
    count_decision(c, _ev("skipped_full", "copy"))        # no room
    count_decision(c, _ev("skipped_no_edge", "copy"))     # didn't want it
    assert c["copy"] == {"no_slot": 1, "declined": 1}


def test_a_trade_no_arm_wanted_is_not_counted():
    """Below the whale threshold, or a wallet between the copy and fade
    bands: no arm formed an intent, so it is not an opportunity denied."""
    c = {}
    count_decision(c, _ev("ignored_too_small", None))
    count_decision(c, _ev("skipped_no_edge", None))
    assert c == {}


def test_every_action_the_strategies_emit_is_classified():
    """An unclassified action silently becomes 'other' and disappears from
    the denial rate."""
    import inspect, re
    from src.strategy import whale_follow, edge_value
    emitted = set()
    for mod in (whale_follow, edge_value):
        emitted |= set(re.findall(r'action="([^"]+)"', inspect.getsource(mod)))
    unclassified = (emitted - set(DENIAL_KINDS) - set(TAKEN_ACTIONS)
                    - set(PRE_INTENT_ACTIONS))
    assert not unclassified, f"actions missing from DENIAL_KINDS: {sorted(unclassified)}"


def test_the_strategies_label_the_stance_they_wanted():
    """A capped explore bet must say 'explore', not arrive as None."""
    import inspect, re
    src = inspect.getsource(__import__("src.strategy.whale_follow", fromlist=["x"]))
    for action in ("skipped_explore_cap", "skipped_full", "skipped_capital"):
        call = re.search(r'action="' + action + r'"[^)]*\)', src, re.S)
        assert call and "stance=" in call.group(0), f"{action} carries no stance"


# -- regimes -----------------------------------------------------------------
def test_the_boundaries_are_ordered_and_terminated():
    bounds = [b for b, _, _ in stats.REGIMES]
    assert bounds == sorted(bounds)
    assert bounds[-1] == float("inf"), "the last regime must catch everything after it"


@pytest.mark.parametrize("when,expected", [
    ("2026-10-15", "newest page only"),
    ("2026-10-25", "feed paging"),
    ("2026-11-03", "outage"),
    ("2026-11-06", "data API v2"),
])
def test_a_bet_lands_in_the_regime_it_was_opened_in(when, expected):
    import calendar, datetime as dt
    ts = calendar.timegm(dt.date.fromisoformat(when).timetuple())
    assert stats.regime_of(ts)[0] == expected


def test_bets_either_side_of_a_boundary_are_not_pooled():
    import calendar
    before = calendar.timegm((2026, 10, 15, 0, 0, 0))
    after = calendar.timegm((2026, 10, 25, 0, 0, 0))
    p = PaperPortfolio(1000.0)
    for ts, pnl in ((before, 10.0), (before, 10.0), (after, -5.0)):
        p.open_or_add(market_id="m", token_id=f"t{ts}{pnl}", outcome="Yes", title="q",
                      price=0.5, usd_amount=10, timestamp=ts, stance="copy")
        p.resolve_position(f"t{ts}{pnl}", won=pnl > 0, timestamp=ts + 60)

    split = stats.regime_split(p)
    assert set(split) == {"newest page only", "feed paging"}
    assert split["newest page only"]["bets"] == 2
    assert split["feed paging"]["bets"] == 1
    assert split["newest page only"]["win_rate"] == pytest.approx(1.0)
    assert split["feed paging"]["win_rate"] == pytest.approx(0.0)


def test_an_empty_portfolio_splits_into_nothing():
    assert stats.regime_split(PaperPortfolio(1000.0)) == {}


# -- the counters have to survive the trip through state --------------------
def test_counters_round_trip_through_the_state_file(tmp_path):
    """They accumulate over weeks across hundreds of separate runs, so a
    counter that is not persisted measures one cycle and resets."""
    from src.experiment import Variant, load_state, save_state
    from src.scorecard import TraderScorecard

    v = Variant(name="x@$500", strategy=None, portfolio=PaperPortfolio(1000.0),
                settings={"profile": "balanced"})
    count_decision(v.capacity, _ev("explore", "explore"))
    count_decision(v.capacity, _ev("skipped_explore_cap", "explore"))
    count_decision(v.capacity, _ev("skipped_explore_cap", "explore"))

    path = tmp_path / "state.json"
    save_state(str(path), [v], TraderScorecard(), 123.0, {}, capacity_since=99.0)

    fresh = Variant(name="x@$500", strategy=None, portfolio=PaperPortfolio(1000.0), settings={})
    _, _, since = load_state(str(path), [fresh], TraderScorecard())
    assert fresh.capacity == {"explore": {"taken": 1, "no_slot": 2}}
    assert since == 99.0


def test_a_new_variant_starts_with_its_own_empty_counters():
    """A shared default dict would pool all 30 bots into one tally."""
    from src.experiment import Variant
    a = Variant(name="a", strategy=None, portfolio=PaperPortfolio(1000.0), settings={})
    b = Variant(name="b", strategy=None, portfolio=PaperPortfolio(1000.0), settings={})
    count_decision(a.capacity, _ev("copy", "copy"))
    assert b.capacity == {}


def test_the_report_renders_from_real_shaped_state(tmp_path, capsys):
    runs = [{
        "capacity_counting_since": 1793000000.0,
        "variants": [
            {"capacity": {"explore": {"taken": 3, "no_slot": 40},
                          "copy": {"taken": 12, "no_slot": 1, "declined": 5}},
             "regimes": {}},
        ],
    }]
    stats.print_capacity(runs)
    out = capsys.readouterr().out
    assert "explore" in out and "copy" in out
    # explore wanted 43 and was refused 40 -> 93.0%
    assert "93.0%" in out
    # copy was filled 12 times against 1 refusal, so only explore is named.
    warning = out.split("-- opportunities")[-1]
    assert "was refused more often than it was filled" in warning
    named = warning.split("\n   ")[-2]
    assert "explore" in named and "copy" not in named
