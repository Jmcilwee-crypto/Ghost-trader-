"""The value bots lost $247 over 42 bets at an average entry of 0.287. Two
compounding causes, both pinned here:

  1. Ranking by raw points of edge always picks the cheapest side, which is
     where a bookmaker-derived probability is least reliable.
  2. Topping up a position each time a signal repeated let stakes grow past
     the per-trade cap, so the most-repeated bets became the largest ones.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio import PaperPortfolio
from src.research.base import ProbabilityEstimate
from tests.test_edge_value import estimate, make_event, make_strategy


def _event_with_prices(price, opp_price):
    e = make_event(price=price)
    e.opposite_price = opp_price
    return e


# ---- longshot bias -----------------------------------------------------------
def test_longshot_no_longer_outranks_a_sensible_favourite():
    # Traded side is a 0.08 longshot research calls 0.22 (+14 points).
    # Opposite side is a 0.92 favourite research calls 0.78 (-14 points).
    # Old code took the longshot on raw points; relative edge plus the price
    # floor must now refuse it rather than buying the unreliable tail.
    strategy = make_strategy(min_edge=0.08, min_relative_edge=0.25, min_value_price=0.15)
    ev = strategy.evaluate(_event_with_prices(0.08, 0.92), PaperPortfolio(1000), None, estimate(0.22))

    assert ev.signal is None
    assert ev.action == "skipped_price_bounds"
    assert "least reliable at the tails" in ev.reason


def test_a_genuinely_underpriced_mid_market_bet_still_fires():
    strategy = make_strategy(min_edge=0.08, min_relative_edge=0.25, min_value_price=0.15)
    ev = strategy.evaluate(make_event(price=0.40), PaperPortfolio(1000), None, estimate(0.62))

    assert ev.signal is not None          # +22 points, +55% relative
    assert ev.signal.stance == "value"


def test_big_points_edge_is_refused_when_relative_edge_is_thin():
    # 0.70 -> 0.80 is +10 points but only +14% relative: not worth the risk of
    # an estimate that is routinely a few points off.
    strategy = make_strategy(min_edge=0.08, min_relative_edge=0.25, min_value_price=0.15)
    ev = strategy.evaluate(make_event(price=0.70), PaperPortfolio(1000), None, estimate(0.80))

    assert ev.signal is None
    assert ev.action == "skipped_no_edge"
    assert "relative" in ev.reason


def test_price_floor_is_independent_of_the_global_bounds():
    # 0.10 clears the global 0.05 floor but not the stricter value-bet floor.
    strategy = make_strategy(min_edge=0.05, min_relative_edge=0.10, min_value_price=0.15)
    ev = strategy.evaluate(_event_with_prices(0.10, 0.90), PaperPortfolio(1000), None, estimate(0.30))
    assert ev.action == "skipped_price_bounds"


# ---- one entry per market ----------------------------------------------------
def test_will_not_top_up_a_market_it_already_holds():
    strategy = make_strategy(min_edge=0.08, min_relative_edge=0.25, min_value_price=0.15)
    portfolio = PaperPortfolio(1000)
    event = make_event(price=0.40)

    first = strategy.evaluate(event, portfolio, None, estimate(0.62))
    assert first.signal is not None
    portfolio.open_or_add(market_id=event.market_id, token_id=first.signal.token_id,
                          outcome=first.signal.outcome, title=event.title, price=first.signal.price,
                          usd_amount=first.signal.usd_amount, timestamp=1, stance="value")

    again = strategy.evaluate(event, portfolio, None, estimate(0.62))
    assert again.signal is None
    assert again.action == "skipped_already_in"


def test_a_different_market_is_still_tradable():
    strategy = make_strategy(min_edge=0.08, min_relative_edge=0.25, min_value_price=0.15)
    portfolio = PaperPortfolio(1000)
    portfolio.open_or_add(market_id="other-market", token_id="x", outcome="Yes", title="q",
                          price=0.5, usd_amount=20, timestamp=1, stance="value")

    ev = strategy.evaluate(make_event(price=0.40), portfolio, None, estimate(0.62))
    assert ev.signal is not None
