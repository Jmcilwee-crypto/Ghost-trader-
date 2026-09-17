import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.portfolio import PaperPortfolio
from src.risk import RiskConfig, RiskManager
from src.scorecard import TraderScorecard
from src.strategy.base import TradeEvent
from src.strategy.whale_follow import WhaleFollowStrategy

CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())


def make_strategy(scorecard=None, **strategy_overrides):
    """Tests that depend on a specific strategy setting must pin it via
    strategy_overrides -- config.yaml is a live, user-tuned file, so reading
    its values directly would make tests fail whenever it's retuned."""
    scorecard = scorecard or TraderScorecard()
    config = copy.deepcopy(CONFIG)
    config["strategy"].update(strategy_overrides)
    risk = RiskManager(RiskConfig(**config["risk"]))
    return WhaleFollowStrategy(config=config, scorecard=scorecard, risk=risk), scorecard


def make_event(wallet="w1", usd_size=5000, price=0.5):
    return TradeEvent(
        wallet=wallet, market_id="m1", title="Will X happen?", token_id="yes", outcome="Yes",
        price=price, usd_size=usd_size, timestamp=1, opposite_token_id="no", opposite_outcome="No",
    )


def test_small_trade_below_whale_threshold_is_ignored():
    strategy, _ = make_strategy()
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(usd_size=10), portfolio)
    assert signal is None


def test_unknown_wallet_skipped_when_explore_disabled():
    strategy, _ = make_strategy(explore_unknown_wallets=False)
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(), portfolio)
    assert signal is None


def test_unknown_wallet_gets_small_explore_bet_when_explore_enabled():
    strategy, _ = make_strategy(explore_unknown_wallets=True, explore_size_usd=15.0)
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(), portfolio)
    assert signal is not None
    assert signal.stance == "explore"
    assert signal.usd_amount == 15.0
    assert signal.token_id == "yes"  # explore copies the whale's direction


def test_good_track_record_copies():
    strategy, sc = make_strategy()
    for i in range(6):
        sc.observe_trade(wallet="w1", market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        sc.resolve_market(f"hist{i}", winning_token_id="yes")  # always right
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(wallet="w1"), portfolio)
    assert signal is not None
    assert signal.stance == "copy"
    assert signal.token_id == "yes"
    assert signal.usd_amount > 0


def test_bad_track_record_fades():
    strategy, sc = make_strategy()
    for i in range(6):
        sc.observe_trade(wallet="w1", market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        sc.resolve_market(f"hist{i}", winning_token_id="no")  # always wrong
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(wallet="w1"), portfolio)
    assert signal is not None
    assert signal.stance == "fade"
    assert signal.token_id == "no"


def test_mediocre_track_record_no_edge_skipped():
    strategy, sc = make_strategy()
    for i in range(6):
        sc.observe_trade(wallet="w1", market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        sc.resolve_market(f"hist{i}", winning_token_id="yes" if i % 2 == 0 else "no")
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(wallet="w1"), portfolio)
    assert signal is None


def test_position_size_respects_max_pct_per_trade():
    strategy, sc = make_strategy()
    for i in range(6):
        sc.observe_trade(wallet="w1", market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        sc.resolve_market(f"hist{i}", winning_token_id="yes")
    portfolio = PaperPortfolio(1000)
    signal = strategy.decide(make_event(wallet="w1"), portfolio)
    max_allowed = 1000 * CONFIG["risk"]["max_pct_per_trade"]
    assert signal.usd_amount <= max_allowed + 1e-6


def test_max_concurrent_positions_blocks_new_signals():
    strategy, sc = make_strategy()
    for i in range(6):
        sc.observe_trade(wallet="w1", market_id=f"hist{i}", token_id="yes", usd_size=100, price=0.5)
        sc.resolve_market(f"hist{i}", winning_token_id="yes")
    portfolio = PaperPortfolio(1_000_000)
    limit = CONFIG["risk"]["max_concurrent_positions"]
    for i in range(limit):
        portfolio.open_or_add(market_id=f"other{i}", token_id=f"t{i}", outcome="Yes", title="q", price=0.5, usd_amount=10, timestamp=1, stance="copy")
    signal = strategy.decide(make_event(wallet="w1"), portfolio)
    assert signal is None
