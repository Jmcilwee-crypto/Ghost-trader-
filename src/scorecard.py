"""Tracks each wallet's track record so the strategy can decide whether to
copy or fade its next big bet.

Trades are "pending" until the market they belong to resolves -- only then
do we know whether the wallet was right. This module deliberately never lets
you query a wallet's accuracy using information from a market that hasn't
resolved yet, which is what keeps the backtest honest (no lookahead bias):
the caller is responsible for calling `resolve_market` only when a market
actually closes, in chronological order.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _PendingBet:
    wallet: str
    token_id: str
    usd_size: float
    price: float


@dataclass
class _WalletStats:
    resolved_count: int = 0
    weighted_wins: float = 0.0
    weighted_total: float = 0.0

    @property
    def accuracy(self) -> float | None:
        if self.weighted_total <= 0:
            return None
        return self.weighted_wins / self.weighted_total


class TraderScorecard:
    def __init__(self):
        self._pending_by_market: dict[str, list[_PendingBet]] = {}
        self._stats: dict[str, _WalletStats] = {}

    def observe_trade(self, *, wallet: str, market_id: str, token_id: str, usd_size: float, price: float) -> None:
        self._pending_by_market.setdefault(market_id, []).append(
            _PendingBet(wallet=wallet, token_id=token_id, usd_size=usd_size, price=price)
        )

    def resolve_market(self, market_id: str, *, winning_token_id: str) -> None:
        pending = self._pending_by_market.pop(market_id, [])
        for bet in pending:
            won = bet.token_id == winning_token_id
            stats = self._stats.setdefault(bet.wallet, _WalletStats())
            stats.resolved_count += 1
            stats.weighted_total += bet.usd_size
            if won:
                stats.weighted_wins += bet.usd_size

    def drop_market(self, market_id: str) -> None:
        """Discard pending bets for a market that will never resolve cleanly
        (e.g. voided/ambiguous), so it doesn't skew anyone's stats."""
        self._pending_by_market.pop(market_id, None)

    def accuracy(self, wallet: str) -> float | None:
        stats = self._stats.get(wallet)
        return stats.accuracy if stats else None

    def resolved_count(self, wallet: str) -> int:
        stats = self._stats.get(wallet)
        return stats.resolved_count if stats else 0

    def has_track_record(self, wallet: str, min_count: int) -> bool:
        return self.resolved_count(wallet) >= min_count

    # ---- persistence --------------------------------------------------------
    # Pending bets MUST be persisted alongside resolved stats. A prediction
    # market can sit open for days, so if the in-flight ledger is dropped on
    # restart then nobody is ever scored when those markets finally settle --
    # and with no track records, the copy/fade strategy can never trigger at
    # all. Losing it silently disables the whole point of the scorecard.
    def to_dict(self) -> dict:
        return {
            "wallets": {
                wallet: {"resolved_count": s.resolved_count, "weighted_wins": s.weighted_wins,
                          "weighted_total": s.weighted_total}
                for wallet, s in self._stats.items()
            },
            "pending": {
                market_id: [{"wallet": b.wallet, "token_id": b.token_id,
                              "usd_size": b.usd_size, "price": b.price} for b in bets]
                for market_id, bets in self._pending_by_market.items()
            },
        }

    def load_dict(self, data: dict) -> None:
        if not data:
            return
        # Older state files stored wallet stats at the top level with no
        # "wallets" key; read those too rather than discarding the history.
        wallets = data.get("wallets") if "wallets" in data or "pending" in data else data
        for wallet, s in (wallets or {}).items():
            self._stats[wallet] = _WalletStats(**s)
        for market_id, bets in (data.get("pending") or {}).items():
            self._pending_by_market[market_id] = [_PendingBet(**b) for b in bets]

    def pending_count(self) -> int:
        return sum(len(b) for b in self._pending_by_market.values())
