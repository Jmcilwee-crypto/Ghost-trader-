import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.spot import (BreakoutStrategy, BuyAndHoldStrategy, MeanReversionStrategy,
                                MomentumStrategy, VolatilityScaledMomentum)


def rising(n=80, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def falling(n=80, start=200.0, step=1.0):
    return [start - i * step for i in range(n)]


# ---- momentum ----------------------------------------------------------------
def test_momentum_buys_an_uptrend():
    d = MomentumStrategy(fast=5, slow=20).decide(rising(), holding=False)
    assert d.action == "buy"
    assert "uptrend" in d.reason


def test_momentum_sells_when_the_trend_breaks():
    d = MomentumStrategy(fast=5, slow=20).decide(falling(), holding=True, entry_price=200.0)
    assert d.action == "sell"


def test_momentum_holds_without_enough_history():
    assert MomentumStrategy(fast=5, slow=50).decide([100.0, 101.0], holding=False).action == "hold"


def test_momentum_does_not_rebuy_what_it_already_holds():
    assert MomentumStrategy(fast=5, slow=20).decide(rising(), holding=True, entry_price=100).action != "buy"


# ---- stop loss / take profit -------------------------------------------------
def test_stop_loss_fires_even_while_the_trend_still_looks_good():
    # Price is rising, so momentum alone would hold -- but we entered far above.
    closes = rising()
    strategy = MomentumStrategy(fast=5, slow=20, stop_loss_pct=0.10)
    d = strategy.decide(closes, holding=True, entry_price=closes[-1] * 1.5)
    assert d.action == "sell"
    assert "Stop loss" in d.reason


def test_take_profit_fires_on_a_big_gain():
    closes = rising()
    strategy = MomentumStrategy(fast=5, slow=20, take_profit_pct=0.20)
    d = strategy.decide(closes, holding=True, entry_price=closes[-1] * 0.5)
    assert d.action == "sell"
    assert "Take profit" in d.reason


def test_risk_exits_do_not_apply_when_flat():
    strategy = MomentumStrategy(fast=5, slow=20, stop_loss_pct=0.01)
    assert strategy.decide(rising(), holding=False, entry_price=None).action == "buy"


# ---- mean reversion ----------------------------------------------------------
def test_mean_reversion_buys_when_oversold():
    d = MeanReversionStrategy(period=14, oversold=30).decide(falling(), holding=False)
    assert d.action == "buy"
    assert "oversold" in d.reason


def test_mean_reversion_sells_once_recovered():
    d = MeanReversionStrategy(period=14, exit_level=55).decide(rising(), holding=True, entry_price=100)
    assert d.action == "sell"


def test_mean_reversion_and_momentum_disagree_on_a_downtrend():
    # The whole point of running both: one must be wrong in any given regime.
    closes = falling()
    assert MeanReversionStrategy().decide(closes, holding=False).action == "buy"
    assert MomentumStrategy(fast=5, slow=20).decide(closes, holding=False).action != "buy"


# ---- breakout ----------------------------------------------------------------
def test_breakout_buys_a_new_high():
    closes = [100.0] * 30 + [130.0]
    d = BreakoutStrategy(lookback=20).decide(closes, holding=False)
    assert d.action == "buy"
    assert "Broke above" in d.reason


def test_breakout_ignores_price_sitting_inside_the_range():
    closes = [100.0] * 30 + [99.0]
    assert BreakoutStrategy(lookback=20).decide(closes, holding=False).action == "hold"


def test_breakout_exits_when_it_fails():
    closes = [100.0] * 30 + [130.0] * 10 + [90.0]
    d = BreakoutStrategy(lookback=20, exit_lookback=5).decide(closes, holding=True, entry_price=130.0)
    assert d.action == "sell"


# ---- baseline ----------------------------------------------------------------
def test_buy_and_hold_buys_once_then_never_sells():
    s = BuyAndHoldStrategy()
    assert s.decide(rising(), holding=False).action == "buy"
    assert s.decide(falling(), holding=True, entry_price=200).action == "hold"


# ---- volatility scaling ------------------------------------------------------
def test_volatility_scaling_shrinks_size_on_a_wild_asset():
    calm = rising(n=80, step=0.5)
    wild = [100.0 + (i * 0.5) + (12 if i % 2 else -12) for i in range(80)]

    calm_buy = VolatilityScaledMomentum(fast=5, slow=20, vol_target=0.02).decide(calm, holding=False)
    wild_buy = VolatilityScaledMomentum(fast=5, slow=20, vol_target=0.02).decide(wild, holding=False)

    assert calm_buy.action == "buy" and wild_buy.action == "buy"
    assert wild_buy.strength < calm_buy.strength


def test_volatility_scaling_leaves_sell_decisions_alone():
    d = VolatilityScaledMomentum(fast=5, slow=20).decide(falling(), holding=True, entry_price=200)
    assert d.action == "sell"
