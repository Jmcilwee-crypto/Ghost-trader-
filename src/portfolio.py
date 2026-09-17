"""In-memory (optionally disk-persisted) paper trading portfolio.

Nothing in this module talks to the network or moves real money -- it is
pure bookkeeping for a simulation. Shares are modeled the way Polymarket
outcome tokens actually work: each share redeems for $1 if its outcome wins
and $0 if it loses, and 0 <= price <= 1 the whole time.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    title: str
    shares: float
    avg_price: float
    opened_at: float
    stance: str  # "copy" or "fade" -- why we entered, for reporting only

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_price


@dataclass
class ClosedTrade:
    market_id: str
    token_id: str
    outcome: str
    title: str
    shares: float
    avg_price: float
    exit_price: float
    opened_at: float
    closed_at: float
    reason: str  # "resolved_win", "resolved_loss", "manual_exit"
    pnl: float
    # Carried over from the Position so finished bets can still be grouped by
    # why they were opened. Defaults for state files written before this
    # existed, which would otherwise fail to load.
    stance: str = "unknown"


class PaperPortfolio:
    # Binary outcome tokens trade strictly between 0 and 1 and settle at $1 or
    # $0. Continuous instruments (crypto, equities) have no upper bound and no
    # settlement, so SpotPortfolio lifts this to None.
    max_price: float | None = 1.0

    def __init__(self, starting_cash: float):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}  # keyed by token_id
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[float, float]] = []  # (timestamp, equity)

    # ---- position management ------------------------------------------------
    def open_or_add(
        self,
        *,
        market_id: str,
        token_id: str,
        outcome: str,
        title: str,
        price: float,
        usd_amount: float,
        timestamp: float,
        stance: str,
    ) -> Position:
        if price <= 0 or (self.max_price is not None and price >= self.max_price):
            raise ValueError(f"price must be in (0, {self.max_price}), got {price}")
        usd_amount = min(usd_amount, self.cash)
        if usd_amount <= 0:
            raise ValueError("insufficient cash to open position")

        shares = usd_amount / price
        self.cash -= usd_amount

        existing = self.positions.get(token_id)
        if existing is None:
            pos = Position(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                title=title,
                shares=shares,
                avg_price=price,
                opened_at=timestamp,
                stance=stance,
            )
            self.positions[token_id] = pos
            return pos

        total_cost = existing.cost_basis + shares * price
        existing.shares += shares
        existing.avg_price = total_cost / existing.shares
        return existing

    def resolve_position(self, token_id: str, *, won: bool, timestamp: float) -> ClosedTrade | None:
        pos = self.positions.pop(token_id, None)
        if pos is None:
            return None
        exit_price = 1.0 if won else 0.0
        proceeds = pos.shares * exit_price
        self.cash += proceeds
        trade = ClosedTrade(
            market_id=pos.market_id,
            token_id=pos.token_id,
            outcome=pos.outcome,
            title=pos.title,
            shares=pos.shares,
            avg_price=pos.avg_price,
            exit_price=exit_price,
            opened_at=pos.opened_at,
            closed_at=timestamp,
            reason="resolved_win" if won else "resolved_loss",
            pnl=proceeds - pos.cost_basis,
            stance=pos.stance,
        )
        self.closed_trades.append(trade)
        return trade

    def close_position_at_price(self, token_id: str, *, exit_price: float, timestamp: float, reason: str) -> ClosedTrade | None:
        pos = self.positions.pop(token_id, None)
        if pos is None:
            return None
        proceeds = pos.shares * exit_price
        self.cash += proceeds
        trade = ClosedTrade(
            market_id=pos.market_id,
            token_id=pos.token_id,
            outcome=pos.outcome,
            title=pos.title,
            shares=pos.shares,
            avg_price=pos.avg_price,
            exit_price=exit_price,
            opened_at=pos.opened_at,
            closed_at=timestamp,
            reason=reason,
            pnl=proceeds - pos.cost_basis,
            stance=pos.stance,
        )
        self.closed_trades.append(trade)
        return trade

    # ---- valuation ------------------------------------------------------------
    def equity(self, current_prices: dict[str, float] | None = None) -> float:
        current_prices = current_prices or {}
        value = self.cash
        for token_id, pos in self.positions.items():
            price = current_prices.get(token_id, pos.avg_price)
            value += pos.shares * price
        return value

    def exposure_in_market(self, market_id: str) -> float:
        return sum(p.cost_basis for p in self.positions.values() if p.market_id == market_id)

    def record_equity(self, timestamp: float, current_prices: dict[str, float] | None = None) -> float:
        eq = self.equity(current_prices)
        self.equity_curve.append((timestamp, eq))
        return eq

    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed_trades)

    def win_rate(self) -> float | None:
        if not self.closed_trades:
            return None
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        return wins / len(self.closed_trades)

    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        max_dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        return max_dd

    def check_stop_condition(self, *, target: float, ruin: float, current_prices: dict[str, float] | None = None) -> str | None:
        eq = self.equity(current_prices)
        if eq >= target:
            return "target"
        if eq <= ruin:
            return "ruin"
        return None

    # ---- persistence ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "closed_trades": [asdict(t) for t in self.closed_trades],
            "equity_curve": self.equity_curve,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PaperPortfolio":
        p = cls(starting_cash=data["starting_cash"])
        p.cash = data["cash"]
        p.positions = {k: Position(**v) for k, v in data["positions"].items()}
        p.closed_trades = [ClosedTrade(**t) for t in data["closed_trades"]]
        p.equity_curve = [tuple(row) for row in data["equity_curve"]]
        return p

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "PaperPortfolio":
        return cls.from_dict(json.loads(Path(path).read_text()))
