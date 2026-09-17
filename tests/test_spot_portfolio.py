import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.portfolio import PaperPortfolio
from src.spot_portfolio import SpotPortfolio


def test_binary_portfolio_still_rejects_prices_outside_zero_to_one():
    p = PaperPortfolio(1000)
    with pytest.raises(ValueError):
        p.open_or_add(market_id="m", token_id="t", outcome="Yes", title="q",
                      price=1.5, usd_amount=10, timestamp=1, stance="copy")


def test_spot_portfolio_accepts_any_positive_price():
    p = SpotPortfolio(1000, fee_rate=0.0)
    pos = p.buy(symbol="BTCUSDT", price=77000.0, usd_amount=500, timestamp=1, strategy="momentum")
    assert pos.avg_price == 77000.0
    assert pos.shares == pytest.approx(500 / 77000.0)


def test_spot_portfolio_still_rejects_nonsense_prices():
    p = SpotPortfolio(1000)
    with pytest.raises(ValueError):
        p.buy(symbol="X", price=0.0, usd_amount=100, timestamp=1, strategy="s")


def test_buy_then_sell_at_a_higher_price_is_profitable():
    p = SpotPortfolio(1000, fee_rate=0.0)
    p.buy(symbol="ETHUSDT", price=2000.0, usd_amount=500, timestamp=1, strategy="momentum")
    trade = p.sell(symbol="ETHUSDT", price=2400.0, timestamp=2, reason="target hit")

    assert trade.pnl == pytest.approx(100.0)   # +20% on $500
    assert p.cash == pytest.approx(1100.0)
    assert p.positions == {}


def test_selling_lower_realizes_a_loss():
    p = SpotPortfolio(1000, fee_rate=0.0)
    p.buy(symbol="ETHUSDT", price=2000.0, usd_amount=500, timestamp=1, strategy="momentum")
    trade = p.sell(symbol="ETHUSDT", price=1800.0, timestamp=2, reason="stop loss")

    assert trade.pnl == pytest.approx(-50.0)
    assert p.cash == pytest.approx(950.0)


def test_fees_are_charged_on_both_entry_and_exit():
    flat = SpotPortfolio(1000, fee_rate=0.0)
    charged = SpotPortfolio(1000, fee_rate=0.01)   # 1% each way, exaggerated

    for p in (flat, charged):
        p.buy(symbol="BTCUSDT", price=100.0, usd_amount=500, timestamp=1, strategy="s")
        p.sell(symbol="BTCUSDT", price=100.0, timestamp=2, reason="flat exit")

    # A round trip at an unchanged price should lose exactly the fees.
    assert flat.equity() == pytest.approx(1000.0)
    assert charged.equity() < flat.equity()
    assert charged.fees_paid > 0
    assert charged.equity() == pytest.approx(1000.0 - charged.fees_paid)


def test_equity_marks_open_positions_to_the_live_price():
    p = SpotPortfolio(1000, fee_rate=0.0)
    p.buy(symbol="BTCUSDT", price=100.0, usd_amount=500, timestamp=1, strategy="s")

    assert p.equity({"BTCUSDT": 100.0}) == pytest.approx(1000.0)
    assert p.equity({"BTCUSDT": 200.0}) == pytest.approx(1500.0)   # position doubled
    assert p.equity({"BTCUSDT": 50.0}) == pytest.approx(750.0)


def test_unrealized_pnl_tracks_the_open_position():
    p = SpotPortfolio(1000, fee_rate=0.0)
    p.buy(symbol="BTCUSDT", price=100.0, usd_amount=500, timestamp=1, strategy="s")
    assert p.unrealized_pnl({"BTCUSDT": 120.0}) == pytest.approx(100.0)
    assert p.unrealized_pnl({}) == 0.0   # no price known -> no claim about value


def test_selling_something_not_held_is_a_no_op():
    p = SpotPortfolio(1000)
    assert p.sell(symbol="NOTHING", price=10.0, timestamp=1, reason="x") is None


def test_closed_trade_records_which_strategy_opened_it():
    p = SpotPortfolio(1000, fee_rate=0.0)
    p.buy(symbol="BTCUSDT", price=100.0, usd_amount=100, timestamp=1, strategy="breakout")
    trade = p.sell(symbol="BTCUSDT", price=110.0, timestamp=2, reason="exit")
    assert trade.stance == "breakout"


def test_persistence_roundtrip_keeps_fees_and_positions():
    p = SpotPortfolio(1000, fee_rate=0.002)
    p.buy(symbol="BTCUSDT", price=100.0, usd_amount=200, timestamp=1, strategy="momentum")
    p.record_equity(1, {"BTCUSDT": 100.0})

    restored = SpotPortfolio.from_dict(p.to_dict())
    assert restored.fee_rate == 0.002
    assert restored.fees_paid == pytest.approx(p.fees_paid)
    assert restored.cash == pytest.approx(p.cash)
    assert restored.positions["BTCUSDT"].shares == pytest.approx(p.positions["BTCUSDT"].shares)
    assert restored.equity_curve == p.equity_curve
