"""Technical indicators, written as plain functions over a list of closes.

No numpy/pandas on purpose: the whole project runs on two dependencies, and
these are a few lines each. Every function returns None rather than raising
when there isn't enough history yet, since the bot will legitimately ask for a
50-period average two minutes after starting up.
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the most recent `period` values."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: list[float], period: int = 14) -> float | None:
    """Relative Strength Index, 0-100. Below ~30 is conventionally 'oversold'.

    Uses Wilder's smoothing (the original formulation) rather than a plain
    average of gains/losses -- a simple average reacts far too sharply and
    would fire different signals than every charting tool the user compares
    against.
    """
    if period <= 0 or len(values) < period + 1:
        return None

    gains, losses = [], []
    for prev, cur in zip(values[-(period + 1):-1], values[-period:]):
        change = cur - prev
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rolling_high(values: list[float], period: int, exclude_last: bool = True) -> float | None:
    """Highest value over the lookback. `exclude_last` leaves out the current
    bar, which is what you want for breakout tests -- otherwise price is always
    trivially equal to its own maximum and every bar looks like a breakout."""
    series = values[:-1] if exclude_last else values
    if period <= 0 or len(series) < period:
        return None
    return max(series[-period:])


def rolling_low(values: list[float], period: int, exclude_last: bool = True) -> float | None:
    series = values[:-1] if exclude_last else values
    if period <= 0 or len(series) < period:
        return None
    return min(series[-period:])


def pct_change(values: list[float], period: int) -> float | None:
    """Percent change over the last `period` bars, as a fraction."""
    if period <= 0 or len(values) < period + 1:
        return None
    old = values[-(period + 1)]
    if old == 0:
        return None
    return (values[-1] - old) / old


def volatility(values: list[float], period: int = 20) -> float | None:
    """Standard deviation of bar-to-bar returns -- used to size positions down
    on assets that move violently."""
    if period <= 1 or len(values) < period + 1:
        return None
    returns = []
    for prev, cur in zip(values[-(period + 1):-1], values[-period:]):
        if prev:
            returns.append((cur - prev) / prev)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return var ** 0.5
