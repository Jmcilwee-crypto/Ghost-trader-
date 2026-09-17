import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy

import yaml

from src.portfolio import PaperPortfolio
from src.research.base import ProbabilityEstimate
from src.risk import RiskConfig, RiskManager
from src.scorecard import TraderScorecard
from src.strategy.base import TradeEvent
from src.strategy.edge_value import EdgeValueStrategy

CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())


def make_strategy(**edge_overrides):
    config = copy.deepcopy(CONFIG)
    config.setdefault("edge_strategy", {})
    config["edge_strategy"].update(edge_overrides)
    config["strategy"]["whale_usd_threshold"] = config["edge_strategy"].get("whale_usd_threshold", 500.0)
    risk = RiskManager(RiskConfig(**config["risk"]))
    return EdgeValueStrategy(config=config, scorecard=TraderScorecard(), risk=risk, research=None)


def make_event(price=0.50, usd_size=5000):
    return TradeEvent(
        wallet="w1", market_id="m1", title="Team A vs. Team B", token_id="yes", outcome="Team A",
        price=price, usd_size=usd_size, timestamp=1, opposite_token_id="no", opposite_outcome="Team B",
    )


def estimate(p, confidence=0.8):
    return ProbabilityEstimate(probability=p, confidence=confidence, source="bookmaker consensus (4 books)")


def test_bets_when_research_says_the_price_is_too_cheap():
    strategy = make_strategy(min_edge=0.08)
    # Market charges 50c; books say it's really a 70% shot -> 20 points of edge.
    ev = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.70))

    assert ev.signal is not None
    assert ev.signal.stance == "value"
    assert ev.signal.token_id == "yes"
    assert ev.possibility_pct == 70.0
    assert "+20.0 points of edge" in ev.reason


def test_skips_when_the_market_is_priced_about_right():
    strategy = make_strategy(min_edge=0.08)
    ev = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.53))

    assert ev.signal is None
    assert ev.action == "skipped_no_edge"


def test_takes_the_other_side_when_that_is_the_underpriced_one():
    strategy = make_strategy(min_edge=0.08)
    # Books say Team A is only a 25% shot, but it's trading at 50c -- so the
    # value is in buying Team B at 50c when it's really a 75% shot.
    ev = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.25))

    assert ev.signal is not None
    assert ev.signal.token_id == "no"
    assert ev.signal.outcome == "Team B"


def test_skips_when_no_research_is_available():
    strategy = make_strategy()
    ev = strategy.evaluate(make_event(), PaperPortfolio(1000), None, None)

    assert ev.signal is None
    assert ev.action == "skipped_no_research"


def test_skips_low_confidence_estimates():
    strategy = make_strategy(min_confidence=0.6)
    ev = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.80, confidence=0.4))

    assert ev.signal is None
    assert ev.action == "skipped_low_confidence"


def test_ignores_trades_below_the_size_threshold():
    strategy = make_strategy(whale_usd_threshold=500.0)
    ev = strategy.evaluate(make_event(usd_size=10), PaperPortfolio(1000), None, estimate(0.90))
    assert ev.action == "ignored_too_small"


def test_bigger_edge_stakes_more_than_smaller_edge():
    strategy = make_strategy(min_edge=0.05, min_relative_edge=0.05)
    small = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.58))
    big = strategy.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, estimate(0.90))

    assert big.signal.usd_amount > small.signal.usd_amount


def test_bolder_kelly_fraction_stakes_more_on_the_same_edge():
    cautious = make_strategy(min_edge=0.05, min_relative_edge=0.05, kelly_fraction=0.25)
    bold = make_strategy(min_edge=0.05, min_relative_edge=0.05, kelly_fraction=0.50)
    e = estimate(0.70)

    a = cautious.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, e)
    b = bold.evaluate(make_event(price=0.50), PaperPortfolio(1000), None, e)
    assert b.signal.usd_amount > a.signal.usd_amount


def test_stake_never_exceeds_the_risk_limit():
    strategy = make_strategy(min_edge=0.01, min_relative_edge=0.05, min_value_price=0.05,
                             kelly_fraction=1.0)
    portfolio = PaperPortfolio(1000)
    ev = strategy.evaluate(make_event(price=0.10), portfolio, None, estimate(0.99))

    cap = 1000 * CONFIG["risk"]["max_pct_per_trade"]
    assert ev.signal.usd_amount <= cap + 1e-6


def test_respects_price_bounds_even_with_a_huge_edge():
    strategy = make_strategy(min_edge=0.01, min_relative_edge=0.01)
    # 0.98 can only pay ~2%, so it must be refused however the edge looks.
    ev = strategy.evaluate(make_event(price=0.98), PaperPortfolio(1000), None, estimate(0.999))
    assert ev.signal is None
    assert ev.action in ("skipped_price_bounds", "skipped_no_edge")


def test_stops_when_at_the_position_limit():
    strategy = make_strategy(min_edge=0.05)
    portfolio = PaperPortfolio(1_000_000)
    for i in range(CONFIG["risk"]["max_concurrent_positions"]):
        portfolio.open_or_add(market_id=f"other{i}", token_id=f"t{i}", outcome="Yes", title="q",
                              price=0.5, usd_amount=10, timestamp=1, stance="value")
    ev = strategy.evaluate(make_event(price=0.50), portfolio, None, estimate(0.80))
    assert ev.signal is None
    assert ev.action == "skipped_full"
