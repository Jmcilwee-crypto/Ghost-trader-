"""Copy/fade-the-whale strategy.

For every trade that's big enough to count as a "whale" signal, look up the
trader's track record (weighted, size-based win rate on resolved bets, via
TraderScorecard). Copy wallets that have historically been right, fade
wallets that have historically been wrong, ignore everyone else -- including
wallets we simply haven't seen enough of yet, unless explore mode is on.

`evaluate()` is the core entry point: it always returns an Evaluation, even
for signals the bot decides to skip, so callers (live.py in particular) can
show a human what was *considered*, not just what was acted on.
`decide()` is a thin convenience wrapper on top of it for callers (backtest)
that only care about the resulting Signal.
"""
from __future__ import annotations

from ..portfolio import PaperPortfolio
from ..risk import RiskManager
from ..scorecard import TraderScorecard
from .base import Evaluation, Signal, TradeEvent


class WhaleFollowStrategy:
    def __init__(self, *, config: dict, scorecard: TraderScorecard, risk: RiskManager):
        s = config["strategy"]
        self.whale_usd_threshold = s["whale_usd_threshold"]
        self.min_track_record = s["min_track_record"]
        self.copy_above = s["copy_above_accuracy"]
        self.fade_below = s["fade_below_accuracy"]
        self.explore_unknown = s["explore_unknown_wallets"]
        self.explore_size_usd = s["explore_size_usd"]
        self.min_entry_price = s.get("min_entry_price", 0.0)
        self.max_entry_price = s.get("max_entry_price", 1.0)
        self.scorecard = scorecard
        self.risk = risk

    def decide(self, event: TradeEvent, portfolio: PaperPortfolio, current_prices: dict[str, float] | None = None,
               estimate=None) -> Signal | None:
        return self.evaluate(event, portfolio, current_prices, estimate).signal

    # `estimate` is accepted and ignored so the experiment can drive every
    # strategy through one call signature; this one trades on track record.
    def evaluate(self, event: TradeEvent, portfolio: PaperPortfolio, current_prices: dict[str, float] | None = None,
                 estimate=None) -> Evaluation:
        importance = self._importance(event.usd_size)
        accuracy = self.scorecard.accuracy(event.wallet)
        track_record_count = self.scorecard.resolved_count(event.wallet)
        has_record = track_record_count >= self.min_track_record
        # Checked per stance: explore bets are held to a tighter sub-cap so
        # they can't occupy every slot and starve copy/fade of room.
        already_in = self.risk.already_in_market(portfolio, event.market_id)
        can_open = self.risk.can_open_new_position(portfolio) and not already_in
        can_explore = self.risk.can_open_new_position(portfolio, stance="explore") and not already_in

        def _eval(*, outcome, price, action, reason, possibility, signal) -> Evaluation:
            return Evaluation(
                wallet=event.wallet, market_id=event.market_id, title=event.title, outcome=outcome, price=price,
                usd_size=event.usd_size, timestamp=event.timestamp, accuracy=accuracy, track_record_count=track_record_count,
                action=action, reason=reason, possibility_pct=possibility, importance_pct=importance, signal=signal,
            )

        def _priced_out(price: float) -> Evaluation | None:
            """Outcome tokens pay a fixed $1, so a share bought at 0.97 can only
            ever gain 3% while still risking the whole stake. Skip both tails
            regardless of how good the signal looks."""
            if self.min_entry_price <= price <= self.max_entry_price:
                return None
            upside_pct = (1 - price) / price * 100 if price > 0 else float("inf")
            return _eval(
                outcome=event.outcome, price=price, action="skipped_price_bounds",
                reason=(f"Entry price {price:.3f} is outside the {self.min_entry_price:g}-{self.max_entry_price:g} range "
                        f"-- a win would pay only {upside_pct:.1f}% while a loss still costs the full stake"),
                possibility=None, signal=None,
            )

        if event.usd_size < self.whale_usd_threshold:
            return _eval(
                outcome=event.outcome, price=event.price, action="ignored_too_small",
                reason=f"Only ${event.usd_size:,.0f}, below the ${self.whale_usd_threshold:,.0f} whale threshold",
                possibility=None, signal=None,
            )

        if not has_record:
            priced_out = _priced_out(event.price)
            if priced_out is not None and self.explore_unknown:
                return priced_out
            if not self.explore_unknown:
                return _eval(
                    outcome=event.outcome, price=event.price, action="skipped_unknown_trader",
                    reason=f"No track record yet for this trader ({track_record_count}/{self.min_track_record} resolved bets seen)",
                    possibility=None, signal=None,
                )
            if not can_explore:
                held = sum(1 for p in portfolio.positions.values() if p.stance == "explore")
                cap = self.risk.config.max_explore_positions
                reason = ("Would try a small explore bet, but already at the max number of open positions"
                          if not can_open else
                          f"Holding {held} explore bets already (cap {cap}) -- the remaining slots are "
                          f"kept free for traders with a real track record")
                return _eval(outcome=event.outcome, price=event.price, action="skipped_explore_cap",
                              reason=reason, possibility=None, signal=None)
            amount = min(self.explore_size_usd, portfolio.cash)
            if amount <= 0:
                return _eval(
                    outcome=event.outcome, price=event.price, action="skipped_capital",
                    reason="Would try a small explore bet, but there's no cash left", possibility=None, signal=None,
                )
            signal = Signal(stance="explore", token_id=event.token_id, outcome=event.outcome, price=event.price, usd_amount=amount, wallet=event.wallet)
            return _eval(
                outcome=event.outcome, price=event.price, action="explore",
                reason="No track record yet for this trader -- placing a small test bet to start scoring them",
                possibility=None, signal=signal,
            )

        if accuracy is not None and accuracy >= self.copy_above:
            possibility = round(accuracy * 100, 1)
            reason_base = f"Copying -- this trader has been right {accuracy:.0%} of the time ({track_record_count} resolved bets)"
            priced_out = _priced_out(event.price)
            if priced_out is not None:
                return priced_out
            if not can_open:
                return _eval(outcome=event.outcome, price=event.price, action="skipped_full",
                              reason=f"{reason_base}, but already at the max number of open positions", possibility=possibility, signal=None)
            amount = self.risk.position_size_usd(portfolio=portfolio, market_id=event.market_id, confidence=_confidence(accuracy), current_prices=current_prices)
            if amount <= 0:
                return _eval(outcome=event.outcome, price=event.price, action="skipped_capital",
                              reason=f"{reason_base}, but risk limits / available cash blocked it", possibility=possibility, signal=None)
            signal = Signal(stance="copy", token_id=event.token_id, outcome=event.outcome, price=event.price, usd_amount=amount, wallet=event.wallet)
            return _eval(outcome=event.outcome, price=event.price, action="copy", reason=reason_base, possibility=possibility, signal=signal)

        if accuracy is not None and accuracy <= self.fade_below:
            possibility = round((1 - accuracy) * 100, 1)
            reason_base = f"Betting against this trader -- they've only been right {accuracy:.0%} of the time ({track_record_count} resolved bets)"
            opp_price = event.opposite_price if event.opposite_price is not None else max(0.01, min(0.99, 1 - event.price))
            priced_out = _priced_out(opp_price)
            if priced_out is not None:
                return priced_out
            if not can_open:
                return _eval(outcome=event.opposite_outcome, price=opp_price, action="skipped_full",
                              reason=f"{reason_base}, but already at the max number of open positions", possibility=possibility, signal=None)
            amount = self.risk.position_size_usd(portfolio=portfolio, market_id=event.market_id, confidence=_confidence(1 - accuracy), current_prices=current_prices)
            if amount <= 0:
                return _eval(outcome=event.opposite_outcome, price=opp_price, action="skipped_capital",
                              reason=f"{reason_base}, but risk limits / available cash blocked it", possibility=possibility, signal=None)
            signal = Signal(stance="fade", token_id=event.opposite_token_id, outcome=event.opposite_outcome, price=opp_price, usd_amount=amount, wallet=event.wallet)
            return _eval(outcome=event.opposite_outcome, price=opp_price, action="fade", reason=reason_base, possibility=possibility, signal=signal)

        possibility = round(accuracy * 100, 1) if accuracy is not None else None
        return _eval(
            outcome=event.outcome, price=event.price, action="skipped_no_edge",
            reason=f"Trader's track record ({accuracy:.0%} right, {track_record_count} bets) isn't strong enough either way" if accuracy is not None else "No usable track record",
            possibility=possibility, signal=None,
        )

    def _importance(self, usd_size: float) -> float:
        """0-100 scale for how notable a signal is, purely by dollar size:
        100 == 4x the whale threshold or larger."""
        if self.whale_usd_threshold <= 0:
            return 0.0
        return round(min(100.0, usd_size / self.whale_usd_threshold * 25), 1)


def _confidence(accuracy: float) -> float:
    """Maps accuracy in [0.5, 1.0] to a 0..1 confidence scalar. Below 0.5
    (shouldn't be passed in directly) clamps to 0."""
    return max(0.0, min(1.0, (accuracy - 0.5) * 2))
