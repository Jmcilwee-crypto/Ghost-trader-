"""Strategies for continuous-price assets.

Each one answers a single question per symbol per cycle: given the price
history and whether we already hold this asset, should we buy, sell, or do
nothing? Position sizing is left to the risk manager, exactly as on the
Polymarket side.

The families deliberately disagree with each other. Momentum buys strength and
mean reversion buys weakness, so whichever market regime shows up, one of them
should be wrong -- that contrast is the point of running them side by side.

Buy-and-hold is included as the control. Without it there's no way to tell
whether a strategy has any skill or just rode the market up.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..indicators import pct_change, rolling_high, rsi, sma, volatility


@dataclass
class SpotDecision:
    action: str          # "buy" | "sell" | "hold"
    reason: str          # plain English, shown in the dashboard
    strength: float = 0.5  # 0-1, scales the position size
    # Set by strategies that scale into a position over time. It's the share
    # of the bot's maximum allocation the position SHOULD be at right now, so
    # the runner buys only the shortfall. Expressed as a target rather than a
    # remembered tranche count so it survives a restart -- the position itself
    # records how far in we already are.
    target_fraction: float | None = None


HOLD_WARMUP = SpotDecision("hold", "Not enough price history yet to judge")


class BaseSpotStrategy:
    name = "base"

    def __init__(self, **params):
        self.params = params
        self.stop_loss_pct = params.get("stop_loss_pct", 0.0)
        self.take_profit_pct = params.get("take_profit_pct", 0.0)

    def _risk_exit(self, closes: list[float], entry_price: float | None) -> SpotDecision | None:
        """Universal stop-loss / take-profit, checked before strategy logic so
        a runaway loss is cut regardless of what the signal still believes."""
        if entry_price is None or not closes:
            return None
        change = (closes[-1] - entry_price) / entry_price
        if self.stop_loss_pct and change <= -self.stop_loss_pct:
            return SpotDecision("sell", f"Stop loss hit -- down {change:.1%} from entry")
        if self.take_profit_pct and change >= self.take_profit_pct:
            return SpotDecision("sell", f"Take profit hit -- up {change:.1%} from entry")
        return None

    # `symbol` is accepted by every strategy so the runner can drive them all
    # through one call; only catalyst-style strategies actually look at it.
    def decide(self, closes: list[float], holding: bool, entry_price: float | None = None,
               symbol: str | None = None) -> SpotDecision:
        raise NotImplementedError


class MomentumStrategy(BaseSpotStrategy):
    """Buy when the fast average crosses above the slow one, exit when it
    crosses back. Rides trends; whipsaws badly in sideways markets."""
    name = "momentum"

    def __init__(self, fast: int = 10, slow: int = 50, **params):
        super().__init__(fast=fast, slow=slow, **params)
        self.fast, self.slow = fast, slow

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        if holding:
            risk = self._risk_exit(closes, entry_price)
            if risk:
                return risk
        fast, slow = sma(closes, self.fast), sma(closes, self.slow)
        if fast is None or slow is None:
            return HOLD_WARMUP

        gap = (fast - slow) / slow if slow else 0.0
        if not holding and fast > slow:
            return SpotDecision("buy", f"{self.fast}-bar average is {gap:.2%} above the {self.slow}-bar -- uptrend",
                                 strength=min(1.0, abs(gap) * 20))
        if holding and fast < slow:
            return SpotDecision("sell", f"{self.fast}-bar average dropped below the {self.slow}-bar -- trend over")
        return SpotDecision("hold", f"Trend unchanged ({self.fast}/{self.slow} gap {gap:+.2%})")


class MeanReversionStrategy(BaseSpotStrategy):
    """Buy when RSI says the asset is oversold, sell once it recovers. The
    opposite bet to momentum: assumes sharp moves overshoot and snap back."""
    name = "mean_reversion"

    def __init__(self, period: int = 14, oversold: float = 30.0, exit_level: float = 55.0, **params):
        super().__init__(period=period, oversold=oversold, exit_level=exit_level, **params)
        self.period, self.oversold, self.exit_level = period, oversold, exit_level

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        if holding:
            risk = self._risk_exit(closes, entry_price)
            if risk:
                return risk
        value = rsi(closes, self.period)
        if value is None:
            return HOLD_WARMUP

        if not holding and value <= self.oversold:
            depth = (self.oversold - value) / max(self.oversold, 1)
            return SpotDecision("buy", f"RSI {value:.0f} -- oversold below {self.oversold:.0f}",
                                 strength=min(1.0, 0.4 + depth))
        if holding and value >= self.exit_level:
            return SpotDecision("sell", f"RSI recovered to {value:.0f} -- back to normal")
        return SpotDecision("hold", f"RSI {value:.0f} -- no signal")


class BreakoutStrategy(BaseSpotStrategy):
    """Buy when price closes above its recent high, exit when it falls back
    below a shorter-term low. Catches big moves early, pays for it in false
    starts."""
    name = "breakout"

    def __init__(self, lookback: int = 20, exit_lookback: int = 10, **params):
        super().__init__(lookback=lookback, exit_lookback=exit_lookback, **params)
        self.lookback, self.exit_lookback = lookback, exit_lookback

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        if holding:
            risk = self._risk_exit(closes, entry_price)
            if risk:
                return risk
        high = rolling_high(closes, self.lookback)
        if high is None:
            return HOLD_WARMUP
        price = closes[-1]

        if not holding and price > high:
            margin = (price - high) / high
            return SpotDecision("buy", f"Broke above its {self.lookback}-bar high by {margin:.2%}",
                                 strength=min(1.0, 0.5 + margin * 30))
        if holding:
            from ..indicators import rolling_low
            low = rolling_low(closes, self.exit_lookback)
            if low is not None and price < low:
                return SpotDecision("sell", f"Fell below the {self.exit_lookback}-bar low -- breakout failed")
        return SpotDecision("hold", f"No breakout (needs a close above {high:,.2f})")


class BuyAndHoldStrategy(BaseSpotStrategy):
    """The control. Buys once and never sells, so every other strategy has to
    prove it beats simply owning the asset."""
    name = "buy_and_hold"

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        if not closes:
            return HOLD_WARMUP
        if not holding:
            return SpotDecision("buy", "Baseline -- buy once and hold, for comparison", strength=1.0)
        return SpotDecision("hold", "Holding the baseline position")


class CatalystStrategy(BaseSpotStrategy):
    """Trades one symbol into a known, dated event.

    Built for the GTA 6 launch (19 Nov 2026) and Take-Two: a single scheduled
    catalyst that the whole market is already staring at. Take-Two has moved
    hard on every GTA headline -- down 7% on the delay, up 13% then down 3%
    when pre-orders opened, and roughly $2.8bn wiped on the leaks -- so its
    daily volatility is several times a normal large cap.

    The interesting question is *when to leave*. "Buy the rumour, sell the
    news" says the run-up is the tradable part and the event itself is where
    anticipation turns into profit-taking; pre-orders opening already produced
    exactly that reaction. `exit_days_before` is what makes that testable:
    set it above zero to sell into the anticipation, or to zero to hold
    through the launch and find out which was right.

    It deliberately ignores every symbol except its own -- it is an
    event bet, not a signal that generalises.
    """
    name = "catalyst"

    def __init__(self, symbol: str, event_date: str, event_name: str = "catalyst",
                 enter_days_before: int = 60, exit_days_before: int = 3,
                 tranches: int = 4, **params):
        super().__init__(symbol=symbol, event_date=event_date, **params)
        self.symbol = symbol
        self.event_name = event_name
        self.event_date = datetime.strptime(event_date, "%Y-%m-%d").date()
        self.enter_days_before = enter_days_before
        self.exit_days_before = exit_days_before
        self.tranches = max(1, tranches)

    def days_to_event(self, today: date | None = None) -> int:
        return (self.event_date - (today or date.today())).days

    def target_fraction(self, days: int) -> float:
        """How much of the full allocation should be on by now. Steps up in
        `tranches` stages across the entry window, so the position is built
        into the run-up rather than committed all at once on day one."""
        progress = 1 - (days / max(self.enter_days_before, 1))
        step = int(progress * self.tranches) + 1
        return min(1.0, max(1, step) / self.tranches)

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        if symbol is not None and symbol != self.symbol:
            return SpotDecision("hold", f"Only trades {self.symbol} around {self.event_name}")
        if not closes:
            return HOLD_WARMUP

        days = self.days_to_event()
        if holding:
            risk = self._risk_exit(closes, entry_price)
            if risk:
                return risk
            if days <= self.exit_days_before:
                return SpotDecision("sell",
                                     f"{self.event_name} is {days} days away -- selling into the "
                                     f"anticipation before the news lands")
            target = self.target_fraction(days)
            step = round(target * self.tranches)
            return SpotDecision("buy",
                                 f"Adding tranche {step}/{self.tranches} -- {days} days to "
                                 f"{self.event_name}, building into the run-up",
                                 strength=target, target_fraction=target)

        if days <= self.exit_days_before:
            return SpotDecision("hold", f"Too late to enter -- {self.event_name} is {days} days away")
        if days > self.enter_days_before:
            return SpotDecision("hold",
                                 f"{self.event_name} is {days} days out, waiting until "
                                 f"{self.enter_days_before} days before")

        target = self.target_fraction(days)
        step = round(target * self.tranches)
        return SpotDecision("buy",
                             f"First tranche ({step}/{self.tranches}) -- {days} days to "
                             f"{self.event_name}, starting the position in {self.symbol}",
                             strength=target, target_fraction=target)


class VolatilityScaledMomentum(MomentumStrategy):
    """Momentum, but stakes less on assets that are swinging wildly. Tests
    whether sizing alone improves a signal that's otherwise identical."""
    name = "vol_scaled_momentum"

    def __init__(self, fast: int = 10, slow: int = 50, vol_period: int = 20,
                 vol_target: float = 0.02, **params):
        super().__init__(fast=fast, slow=slow, **params)
        self.vol_period, self.vol_target = vol_period, vol_target

    def decide(self, closes, holding, entry_price=None, symbol=None) -> SpotDecision:
        decision = super().decide(closes, holding, entry_price, symbol)
        if decision.action != "buy":
            return decision
        vol = volatility(closes, self.vol_period)
        if vol is None or vol <= 0:
            return decision
        scale = max(0.15, min(1.0, self.vol_target / vol))
        moved = pct_change(closes, self.vol_period)
        return SpotDecision(
            "buy",
            f"{decision.reason}; volatility {vol:.2%} vs {self.vol_target:.2%} target -> sizing at {scale:.0%}"
            + (f" (moved {moved:+.1%})" if moved is not None else ""),
            strength=decision.strength * scale,
        )
