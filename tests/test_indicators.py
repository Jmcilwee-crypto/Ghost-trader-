import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.indicators import pct_change, rolling_high, rolling_low, rsi, sma, volatility


def test_sma_averages_the_last_n_values():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2, 3, 4, 5], 2) == 4.5


def test_sma_returns_none_without_enough_history():
    assert sma([1, 2], 5) is None
    assert sma([], 3) is None
    assert sma([1, 2, 3], 0) is None


def test_rsi_is_100_when_price_only_rises():
    assert rsi([float(i) for i in range(1, 40)], 14) == 100.0


def test_rsi_is_0_when_price_only_falls():
    assert rsi([float(i) for i in range(40, 1, -1)], 14) == 0.0


def test_rsi_sits_near_the_middle_for_choppy_prices():
    alternating = [100.0, 101.0] * 20
    value = rsi(alternating, 14)
    assert 40 < value < 60


def test_rsi_needs_one_more_point_than_its_period():
    assert rsi([1.0] * 14, 14) is None      # 14 closes = only 13 changes
    assert rsi([1.0] * 15, 14) is not None


def test_rolling_high_excludes_the_current_bar_by_default():
    # Without excluding it, the latest bar is trivially its own high and every
    # bar would register as a breakout.
    values = [10.0, 12.0, 11.0, 20.0]
    assert rolling_high(values, 3) == 12.0
    assert rolling_high(values, 3, exclude_last=False) == 20.0


def test_rolling_low_excludes_the_current_bar_by_default():
    values = [10.0, 12.0, 11.0, 5.0]
    assert rolling_low(values, 3) == 10.0
    assert rolling_low(values, 3, exclude_last=False) == 5.0


def test_rolling_helpers_return_none_without_enough_history():
    assert rolling_high([1.0, 2.0], 5) is None
    assert rolling_low([1.0, 2.0], 5) is None


def test_pct_change_measures_across_the_window():
    assert pct_change([100.0, 110.0], 1) == pytest.approx(0.10)
    assert pct_change([100.0, 50.0], 1) == pytest.approx(-0.50)


def test_pct_change_guards_divide_by_zero():
    assert pct_change([0.0, 5.0], 1) is None


def test_volatility_is_zero_for_a_flat_series_and_positive_when_moving():
    flat = [100.0] * 25
    assert volatility(flat, 20) == pytest.approx(0.0)

    choppy = [100.0 + (5 if i % 2 else -5) for i in range(25)]
    assert volatility(choppy, 20) > 0.01


def test_volatility_returns_none_without_enough_history():
    assert volatility([1.0, 2.0], 20) is None
