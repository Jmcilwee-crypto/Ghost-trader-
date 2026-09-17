"""Paper portfolio for continuous-price instruments (crypto, equities).

The Polymarket portfolio holds binary contracts: bought between 0 and 1, they
settle at exactly $1 or $0 and the position simply disappears. Spot assets
work nothing like that -- there is no settlement, so a position is only closed
by *selling* it at whatever the market happens to be, and until then its value
floats with the price.

Two consequences drive this class:

  - **Unrealized P&L actually matters.** A Polymarket position was carried at
    cost until it resolved; a spot position has to be marked to the live price
    every cycle or the equity curve is fiction.
  - **Fees are charged twice**, on the way in and the way out. At a few dozen
    basis points a round trip they are easily the difference between a
    strategy that looks profitable and one that is.

It deliberately keeps the same public shape as PaperPortfolio so the stats
module and the dashboard read it without knowing which kind it is.
"""
from __future__ import annotations

from .portfolio import ClosedTrade, PaperPortfolio


class SpotPortfolio(PaperPortfolio):
    # Any positive price is valid -- BTC at $77,000 is a perfectly good entry.
    max_price = None

    def __init__(self, starting_cash: float, fee_rate: float = 0.001):
        super().__init__(starting_cash)
        self.fee_rate = fee_rate
        self.fees_paid = 0.0

    def buy(self, *, symbol: str, price: float, usd_amount: float, timestamp: float,
            strategy: str, name: str | None = None):
        """Spend usd_amount (fee inclusive) on `symbol` at `price`."""
        usd_amount = min(usd_amount, self.cash)
        if usd_amount <= 0:
            raise ValueError("insufficient cash to open position")
        fee = usd_amount * self.fee_rate
        invested = usd_amount - fee
        if invested <= 0:
            raise ValueError("amount too small to cover fees")

        self.fees_paid += fee
        self.cash -= fee  # open_or_add deducts the rest
        return self.open_or_add(
            market_id=symbol, token_id=symbol, outcome="long", title=name or symbol,
            price=price, usd_amount=invested, timestamp=timestamp, stance=strategy,
        )

    def sell(self, *, symbol: str, price: float, timestamp: float, reason: str) -> ClosedTrade | None:
        """Exit the whole position at `price`, net of the exit fee."""
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        gross = pos.shares * price
        fee = gross * self.fee_rate
        self.fees_paid += fee

        trade = self.close_position_at_price(symbol, exit_price=price, timestamp=timestamp, reason=reason)
        if trade is None:
            return None
        # close_position_at_price credited the gross proceeds; take the fee off
        # the cash and the recorded P&L so both tell the same story.
        self.cash -= fee
        trade.pnl -= fee
        return trade

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            price = prices.get(symbol)
            if price is not None:
                total += pos.shares * price - pos.cost_basis
        return total

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["fee_rate"] = self.fee_rate
        data["fees_paid"] = self.fees_paid
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SpotPortfolio":
        base = PaperPortfolio.from_dict(data)
        p = cls(starting_cash=data["starting_cash"], fee_rate=data.get("fee_rate", 0.001))
        p.cash = base.cash
        p.positions = base.positions
        p.closed_trades = base.closed_trades
        p.equity_curve = base.equity_curve
        p.fees_paid = data.get("fees_paid", 0.0)
        return p
