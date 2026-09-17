"""Position sizing and exposure limits, kept separate from strategy logic so
the "should we trade" decision and "how much" decision can be reasoned about
independently."""
from __future__ import annotations

from dataclasses import dataclass

from .portfolio import PaperPortfolio


@dataclass
class RiskConfig:
    max_pct_per_trade: float
    max_concurrent_positions: int
    max_pct_per_market: float
    # Ceiling on how many slots speculative "explore" bets may occupy. Without
    # it, scouting bets fill every slot and sit there for days, so a genuinely
    # good copy/fade signal arrives with nowhere to go -- the exploration
    # crowds out the strategy it exists to enable. None disables the sub-cap.
    max_explore_positions: int | None = None


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config

    def already_in_market(self, portfolio: PaperPortfolio, market_id: str) -> bool:
        """One entry per market per bot. Topping up an existing position each
        time a signal repeats let a stake grow past the per-trade cap toward
        the per-market one, so the bets the bot repeated most -- not the ones
        it was most right about -- ended up largest."""
        return any(p.market_id == market_id for p in portfolio.positions.values())

    def can_open_new_position(self, portfolio: PaperPortfolio, stance: str | None = None) -> bool:
        if len(portfolio.positions) >= self.config.max_concurrent_positions:
            return False
        if stance == "explore" and self.config.max_explore_positions is not None:
            held = sum(1 for p in portfolio.positions.values() if p.stance == "explore")
            return held < self.config.max_explore_positions
        return True

    def position_size_usd(
        self,
        *,
        portfolio: PaperPortfolio,
        market_id: str,
        confidence: float,
        current_prices: dict[str, float] | None = None,
    ) -> float:
        """confidence in [0,1] scales the base bet size; 0 -> no trade."""
        confidence = max(0.0, min(1.0, confidence))
        if confidence <= 0:
            return 0.0

        equity = portfolio.equity(current_prices)
        base = equity * self.config.max_pct_per_trade * confidence

        market_room = equity * self.config.max_pct_per_market - portfolio.exposure_in_market(market_id)
        base = min(base, max(0.0, market_room))
        base = min(base, portfolio.cash)
        return max(0.0, base)
