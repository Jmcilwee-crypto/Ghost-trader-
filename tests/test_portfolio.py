import sys
from pathlib import Path

import pytest

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
    # The equity curve is deliberately not persisted any more -- what has to
    # survive a restart is the statistic derived from it.
    assert loaded.max_drawdown() == pytest.approx(p.max_drawdown())
    assert loaded.peak_equity == pytest.approx(p.peak_equity)


def test_drawdown_survives_a_restart_mid_slump(tmp_path):
    """Reloading must not reset the peak, or a bot that has already fallen
    30% off its high would report a fresh 0% drawdown."""
    p = PaperPortfolio(1000.0)
    for eq in (1000.0, 1500.0, 1050.0):
        p.cash = eq
        p.record_equity(1)
    assert p.max_drawdown() == pytest.approx(0.30)

    path = tmp_path / "state.json"
    p.save(path)
    loaded = PaperPortfolio.load(path)
    assert loaded.max_drawdown() == pytest.approx(0.30)

    # A recovery that does not exceed the old peak must not shrink the record.
    loaded.cash = 1400.0
    loaded.record_equity(2)
    assert loaded.max_drawdown() == pytest.approx(0.30)


def test_legacy_state_with_a_stored_curve_still_restores_its_drawdown():
    """State written before the curve was dropped must not lose weeks of it."""
    data = {
        "starting_cash": 1000.0, "cash": 800.0, "positions": {}, "closed_trades": [],
        "equity_curve": [[1, 1000.0], [2, 2000.0], [3, 1200.0], [4, 1500.0]],
    }
    restored = PaperPortfolio.from_dict(data)
    assert restored.max_drawdown() == pytest.approx(0.40)   # 2000 -> 1200
    assert restored.peak_equity == pytest.approx(2000.0)
    # ...and it is not written back out again.
    assert "equity_curve" not in restored.to_dict()


def test_state_carries_no_equity_curve():
    """70% of the committed experiment state was plot points -- rewritten in
    full every scheduled run, and read back only for one float."""
    p = PaperPortfolio(1000.0)
    for eq in (1000.0, 1200.0, 900.0):
        p.cash = eq
        p.record_equity(1)
    d = p.to_dict()
    assert "equity_curve" not in d
    assert d["max_drawdown"] == pytest.approx(0.25)
    # Still there in memory for the backtest's CSV export.
    assert len(p.equity_curve) == 3
