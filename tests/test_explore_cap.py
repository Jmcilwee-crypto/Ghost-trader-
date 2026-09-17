"""Explore bets used to fill every position slot and sit there for days, so a
copy/fade signal from a trader with a real track record arrived with nowhere
to go. These pin the reserved-slot behaviour that fixes it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio import PaperPortfolio
from src.risk import RiskConfig, RiskManager
from src.scorecard import TraderScorecard
from tests.test_strategy import make_event, make_strategy


def _fill(portfolio, n, stance, start=0):
    for i in range(n):
        portfolio.open_or_add(market_id=f"fill{start + i}", token_id=f"t{start + i}", outcome="Yes",
                              title="q", price=0.5, usd_amount=5, timestamp=1, stance=stance)


def _good_wallet(scorecard, wallet="w1", n=6):
    for i in range(n):
        scorecard.observe_trade(wallet=wallet, market_id=f"h{i}", token_id="yes", usd_size=100, price=0.5)
        scorecard.resolve_market(f"h{i}", winning_token_id="yes")
    return scorecard


# ---- risk manager ------------------------------------------------------------
def test_explore_blocked_at_its_sub_cap_while_others_still_allowed():
    risk = RiskManager(RiskConfig(max_pct_per_trade=.05, max_concurrent_positions=15,
                                   max_pct_per_market=.10, max_explore_positions=7))
    p = PaperPortfolio(1000)
    _fill(p, 7, "explore")

    assert risk.can_open_new_position(p, stance="explore") is False
    assert risk.can_open_new_position(p) is True          # copy/fade still has room
    assert risk.can_open_new_position(p, stance="copy") is True


def test_total_cap_still_wins_over_the_sub_cap():
    risk = RiskManager(RiskConfig(max_pct_per_trade=.05, max_concurrent_positions=10,
                                   max_pct_per_market=.10, max_explore_positions=7))
    p = PaperPortfolio(1000)
    _fill(p, 3, "explore")
    _fill(p, 7, "copy", start=3)          # 10 total -> full

    assert risk.can_open_new_position(p) is False
    assert risk.can_open_new_position(p, stance="explore") is False


def test_sub_cap_can_be_disabled():
    risk = RiskManager(RiskConfig(max_pct_per_trade=.05, max_concurrent_positions=15,
                                   max_pct_per_market=.10, max_explore_positions=None))
    p = PaperPortfolio(1000)
    _fill(p, 12, "explore")
    assert risk.can_open_new_position(p, stance="explore") is True


# ---- strategy ----------------------------------------------------------------
def test_explore_stops_at_the_cap():
    strategy, _ = make_strategy(explore_unknown_wallets=True)
    strategy.risk.config.max_explore_positions = 3
    p = PaperPortfolio(1000)
    _fill(p, 3, "explore")

    ev = strategy.evaluate(make_event(wallet="unknown"), p)
    assert ev.signal is None
    assert ev.action == "skipped_explore_cap"
    assert "kept free" in ev.reason


def test_copy_still_fires_when_explore_slots_are_full():
    # The whole point: a trusted wallet must still get a slot even though
    # scouting bets have taken their share.
    sc = _good_wallet(TraderScorecard())
    strategy, _ = make_strategy(sc, explore_unknown_wallets=True)
    strategy.risk.config.max_explore_positions = 3
    p = PaperPortfolio(1000)
    _fill(p, 3, "explore")

    ev = strategy.evaluate(make_event(wallet="w1", price=0.5), p)
    assert ev.signal is not None
    assert ev.signal.stance == "copy"


def test_copy_is_still_blocked_when_everything_is_full():
    sc = _good_wallet(TraderScorecard())
    strategy, _ = make_strategy(sc)
    cap = strategy.risk.config.max_concurrent_positions
    p = PaperPortfolio(1_000_000)
    _fill(p, cap, "copy")

    ev = strategy.evaluate(make_event(wallet="w1"), p)
    assert ev.signal is None
    assert ev.action == "skipped_full"
