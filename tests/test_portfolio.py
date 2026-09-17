import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.portfolio import PaperPortfolio


def test_open_position_deducts_cash_and_creates_shares():
    p = PaperPortfolio(1000.0)
    pos = p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="Will X happen?", price=0.4, usd_amount=100, timestamp=1, stance="copy")
    assert p.cash == 900.0
    assert pos.shares == 250.0
    assert pos.avg_price == 0.4


def test_open_or_add_averages_price_on_second_buy():
    p = PaperPortfolio(1000.0)
    p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.4, usd_amount=100, timestamp=1, stance="copy")
    pos = p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.6, usd_amount=60, timestamp=2, stance="copy")
    # 250 shares @0.4 (cost 100) + 100 shares @0.6 (cost 60) = 350 shares, cost 160
    assert pos.shares == 350.0
    assert round(pos.avg_price, 4) == round(160 / 350, 4)


def test_resolve_position_win_pays_full_dollar_per_share():
    p = PaperPortfolio(1000.0)
    p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.5, usd_amount=100, timestamp=1, stance="copy")
    trade = p.resolve_position("t1", won=True, timestamp=2)
    assert trade.pnl == 100.0  # 200 shares * $1 - $100 cost
    assert p.cash == 1000.0 + 100.0
    assert "t1" not in p.positions


def test_resolve_position_loss_pays_zero():
    p = PaperPortfolio(1000.0)
    p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.5, usd_amount=100, timestamp=1, stance="copy")
    trade = p.resolve_position("t1", won=False, timestamp=2)
    assert trade.pnl == -100.0
    assert p.cash == 900.0


def test_cannot_spend_more_than_cash_available():
    p = PaperPortfolio(50.0)
    pos = p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.5, usd_amount=500, timestamp=1, stance="copy")
    assert p.cash == 0.0
    assert pos.shares == 100.0  # capped to the $50 actually available


def test_stop_condition_target_and_ruin():
    p = PaperPortfolio(1000.0)
    assert p.check_stop_condition(target=10000, ruin=50) is None
    p.cash = 10500
    assert p.check_stop_condition(target=10000, ruin=50) == "target"
    p.cash = 10
    assert p.check_stop_condition(target=10000, ruin=50) == "ruin"


def test_persistence_roundtrip(tmp_path):
    p = PaperPortfolio(1000.0)
    p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="q", price=0.5, usd_amount=100, timestamp=1, stance="copy")
    p.record_equity(1)
    path = tmp_path / "state.json"
    p.save(path)
    loaded = PaperPortfolio.load(path)
    assert loaded.cash == p.cash
    assert loaded.positions["t1"].shares == p.positions["t1"].shares
    assert loaded.equity_curve == p.equity_curve
