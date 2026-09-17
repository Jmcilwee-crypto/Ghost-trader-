"""Outcome tokens redeem at a fixed $1, so entry price alone caps the upside:
buy at 0.97 and the best case is +3% while the worst is still -100%. These
tests pin the guard that keeps the bot out of both tails."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio import PaperPortfolio
from src.scorecard import TraderScorecard
from tests.test_strategy import make_event, make_strategy


def _with_track_record(scorecard, wallet="w1", right=True, n=6):
    for i in range(n):
        scorecard.observe_trade(wallet=wallet, market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        scorecard.resolve_market(f"hist{i}", winning_token_id="yes" if right else "no")
    return scorecard


def test_copy_is_skipped_when_price_leaves_almost_no_upside():
    sc = _with_track_record(TraderScorecard(), right=True)
    strategy, _ = make_strategy(sc, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.999), PaperPortfolio(1000))

    assert evaluation.signal is None
    assert evaluation.action == "skipped_price_bounds"
    assert "0.1%" in evaluation.reason  # spells out how little a win would pay


def test_copy_is_skipped_when_price_is_near_hopeless():
    sc = _with_track_record(TraderScorecard(), right=True)
    strategy, _ = make_strategy(sc, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.01), PaperPortfolio(1000))

    assert evaluation.signal is None
    assert evaluation.action == "skipped_price_bounds"


def test_copy_still_allowed_inside_the_bounds():
    sc = _with_track_record(TraderScorecard(), right=True)
    strategy, _ = make_strategy(sc, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.60), PaperPortfolio(1000))

    assert evaluation.signal is not None
    assert evaluation.signal.stance == "copy"


def test_explore_bets_respect_the_bounds_too():
    strategy, _ = make_strategy(explore_unknown_wallets=True, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="unknown", price=0.999), PaperPortfolio(1000))

    assert evaluation.signal is None
    assert evaluation.action == "skipped_price_bounds"


def test_fade_checks_the_price_of_the_side_it_actually_buys():
    # The whale bought at 0.02, so fading means buying the opposite side at
    # ~0.98 -- the bound must catch that, not the 0.02 we merely observed.
    sc = _with_track_record(TraderScorecard(), right=False)
    strategy, _ = make_strategy(sc, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.02), PaperPortfolio(1000))

    assert evaluation.signal is None
    assert evaluation.action == "skipped_price_bounds"
    assert "0.980" in evaluation.reason


def test_fade_allowed_when_the_opposite_side_is_sensibly_priced():
    sc = _with_track_record(TraderScorecard(), right=False)
    strategy, _ = make_strategy(sc, min_entry_price=0.05, max_entry_price=0.95)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.40), PaperPortfolio(1000))

    assert evaluation.signal is not None
    assert evaluation.signal.stance == "fade"


def test_bounds_default_to_wide_open_when_unset():
    sc = _with_track_record(TraderScorecard(), right=True)
    strategy, _ = make_strategy(sc, min_entry_price=0.0, max_entry_price=1.0)
    evaluation = strategy.evaluate(make_event(wallet="w1", price=0.999), PaperPortfolio(1000))

    assert evaluation.signal is not None  # no guard configured -> old behaviour
