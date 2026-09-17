"""Shared data types between backtest.py and live.py. Only binary (two
outcome) markets are supported -- "fade" needs a well-defined opposite side,
which multi-outcome markets don't have."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TradeEvent:
    wallet: str
    market_id: str
    title: str
    token_id: str          # outcome token the wallet bought
    outcome: str            # e.g. "Yes"
    price: float             # price paid, in (0, 1)
    usd_size: float
    timestamp: float
    opposite_token_id: str
    opposite_outcome: str
    opposite_price: float | None = None  # None -> caller should approximate as 1 - price


@dataclass
class Signal:
    stance: str        # "copy" or "fade" or "explore"
    token_id: str
    outcome: str
    price: float
    usd_amount: float
    wallet: str          # whose trade triggered this


@dataclass
class Evaluation:
    """Every whale-sized trade the strategy looks at gets one of these, even
    if it decides not to act -- this is what powers the dashboard's
    "flagged/potential trades" panel, so a human can see what the bot noticed
    and why it did or didn't act on it."""
    wallet: str
    market_id: str
    title: str
    outcome: str
    price: float
    usd_size: float
    timestamp: float
    accuracy: float | None            # trader's historical weighted win rate, if known
    track_record_count: int            # how many resolved bets we've seen from this wallet
    action: str                          # "copy" | "fade" | "explore" | "skipped_no_edge" |
                                          # "skipped_unknown_trader" | "skipped_full" | "skipped_capital"
    reason: str                          # human-readable explanation
    possibility_pct: float | None      # our modeled odds this specific bet pays off, 0-100
    importance_pct: float                # how notable the signal is by dollar size, 0-100
    signal: "Signal | None"              # non-None only when action is copy/fade/explore
