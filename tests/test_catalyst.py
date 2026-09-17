"""The catalyst bot bets a single dated event (the GTA 6 launch on
2026-11-19) on a single symbol. The two things that can silently ruin it are
trading the wrong symbol and mistiming the exit, so both are pinned here."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.spot_experiment import RISKY_SPOT_VARIANTS, build_spot_variants
from src.strategy.spot import CatalystStrategy

CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())
PRICES = [100.0 + i for i in range(60)]


def _strategy(days_away, **kw):
    event = date.today() + timedelta(days=days_away)
    return CatalystStrategy(symbol="TTWO", event_date=event.isoformat(),
                            event_name="the GTA 6 launch", **kw)


def test_ignores_every_other_symbol():
    s = _strategy(30)
    assert s.decide(PRICES, holding=False, symbol="AAPL").action == "hold"
    assert s.decide(PRICES, holding=False, symbol="TTWO").action == "buy"


def test_waits_until_inside_the_entry_window():
    far = _strategy(120, enter_days_before=60)
    assert far.decide(PRICES, holding=False, symbol="TTWO").action == "hold"

    near = _strategy(45, enter_days_before=60)
    assert near.decide(PRICES, holding=False, symbol="TTWO").action == "buy"


def test_sizes_up_as_the_event_approaches():
    early = _strategy(55, enter_days_before=60).decide(PRICES, holding=False, symbol="TTWO")
    late = _strategy(5, enter_days_before=60).decide(PRICES, holding=False, symbol="TTWO")
    assert late.strength > early.strength


def test_sell_into_variant_exits_before_the_news():
    s = _strategy(2, exit_days_before=3)
    d = s.decide(PRICES, holding=True, entry_price=100.0, symbol="TTWO")
    assert d.action == "sell"
    assert "anticipation" in d.reason


def test_hold_through_variant_stays_in_until_the_day():
    # It may still be topping up to its target, so the contract that matters is
    # that it does NOT sell before the day -- unlike the sell-into twin.
    s = _strategy(2, exit_days_before=0)
    assert s.decide(PRICES, holding=True, entry_price=100.0, symbol="TTWO").action != "sell"
    # On the day itself it lets go.
    assert _strategy(0, exit_days_before=0).decide(PRICES, holding=True, entry_price=100.0,
                                                    symbol="TTWO").action == "sell"


def test_will_not_open_a_new_position_once_the_window_has_closed():
    s = _strategy(1, exit_days_before=3)
    assert s.decide(PRICES, holding=False, symbol="TTWO").action == "hold"


def test_stop_loss_still_applies_before_the_event():
    s = _strategy(30, stop_loss_pct=0.10)
    d = s.decide(PRICES, holding=True, entry_price=PRICES[-1] * 1.5, symbol="TTWO")
    assert d.action == "sell"
    assert "Stop loss" in d.reason


def test_days_to_event_counts_down():
    assert _strategy(10).days_to_event() == 10


# ---- wiring ------------------------------------------------------------------
def test_risky_tier_is_opt_in():
    base = build_spot_variants(CONFIG, include_risky=False)
    risky = build_spot_variants(CONFIG, include_risky=True)
    assert len(risky) == len(base) + len(RISKY_SPOT_VARIANTS)
    assert not any(v.name.startswith("risky-") for v in base)


def test_risky_bots_get_their_own_larger_limits():
    variants = {v.name: v for v in build_spot_variants(CONFIG, include_risky=True)}
    risky = variants["risky-nostop-5/20"]
    normal = variants["momentum-10/50"]

    assert risky.risk is not None                   # own limits
    assert normal.risk is None                      # shares the conservative default
    assert risky.risk.config.max_pct_per_trade > CONFIG["risk"]["max_pct_per_trade"]


def test_catalyst_builds_both_a_sell_into_and_hold_through_twin():
    cats = [{"symbol": "TTWO", "name": "the GTA 6 launch", "date": "2026-11-19", "exit_days_before": 3}]
    variants = {v.name: v for v in build_spot_variants(CONFIG, catalysts=cats)}

    assert "catalyst-ttwo-sell-into" in variants
    assert "catalyst-ttwo-hold-through" in variants
    assert variants["catalyst-ttwo-sell-into"].strategy.exit_days_before == 3
    assert variants["catalyst-ttwo-hold-through"].strategy.exit_days_before == 0


def test_each_bot_keeps_an_independent_portfolio():
    variants = build_spot_variants(CONFIG, include_risky=True)
    variants[0].portfolio.cash = 1.0
    assert variants[1].portfolio.cash == CONFIG["bankroll"]["starting_usd"]


# ---- scaling in --------------------------------------------------------------
def test_target_allocation_steps_up_across_the_window():
    s = _strategy(60, enter_days_before=60, tranches=4)
    # 60 days out is the first quarter; by the event it should be fully on.
    assert s.target_fraction(60) == pytest.approx(0.25)
    assert s.target_fraction(45) == pytest.approx(0.50)
    assert s.target_fraction(30) == pytest.approx(0.75)
    assert s.target_fraction(15) == pytest.approx(1.00)
    assert s.target_fraction(2) == pytest.approx(1.00)   # never exceeds full


def test_buys_again_while_under_target():
    s = _strategy(30, enter_days_before=60, tranches=4)
    d = s.decide(PRICES, holding=True, entry_price=100.0, symbol="TTWO")
    assert d.action == "buy"                      # tops up rather than idling
    assert d.target_fraction == pytest.approx(0.75)
    assert "tranche" in d.reason


def test_first_entry_reports_a_target_too():
    d = _strategy(60, enter_days_before=60, tranches=4).decide(PRICES, holding=False, symbol="TTWO")
    assert d.action == "buy"
    assert d.target_fraction == pytest.approx(0.25)   # starts at a quarter, not all-in


def test_scaling_in_never_overrides_the_exit():
    # Once inside the exit window the bot must sell, not keep adding.
    d = _strategy(2, exit_days_before=3, tranches=4).decide(PRICES, holding=True,
                                                            entry_price=100.0, symbol="TTWO")
    assert d.action == "sell"


def test_stop_loss_beats_scaling_in():
    s = _strategy(30, enter_days_before=60, tranches=4, stop_loss_pct=0.10)
    d = s.decide(PRICES, holding=True, entry_price=PRICES[-1] * 1.5, symbol="TTWO")
    assert d.action == "sell"
    assert "Stop loss" in d.reason


def test_single_tranche_behaves_like_the_old_one_shot_entry():
    s = _strategy(50, enter_days_before=60, tranches=1)
    assert s.target_fraction(50) == pytest.approx(1.0)
