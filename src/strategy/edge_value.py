"""Value betting: bet when outside research disagrees with the market price.

Where the whale strategy asks "is this trader usually right?", this asks the
more direct question: "is this priced wrong?" A Polymarket share costs `q` and
pays $1 if it hits, so the market is implicitly claiming the probability is
`q`. If bookmakers say the real probability is `p`, then any gap `p - q` is
expected profit per share.

Stake sizing uses the Kelly criterion, which for a binary contract paying $1
reduces to f* = (p - q) / (1 - q). Full Kelly is famously too aggressive and
assumes `p` is exactly right -- it isn't, it's an estimate -- so this applies a
fraction of it and then hands the number to the same risk limits every other
strategy obeys.
"""
from __future__ import annotations

from ..portfolio import PaperPortfolio
from ..research.base import ProbabilityEstimate
from ..risk import RiskManager
from ..scorecard import TraderScorecard
from .base import Evaluation, Signal, TradeEvent


class EdgeValueStrategy:
    def __init__(self, *, config: dict, scorecard: TraderScorecard, risk: RiskManager, research):
        s = config["strategy"]
        e = config.get("edge_strategy", {})
        self.whale_usd_threshold = s["whale_usd_threshold"]
        self.min_entry_price = s.get("min_entry_price", 0.0)
        self.max_entry_price = s.get("max_entry_price", 1.0)
        # How much cheaper than fair the price must be before it's worth it --
        # this has to clear both estimate error and the spread.
        self.min_edge = e.get("min_edge", 0.08)
        # Raw points of edge systematically favour longshots: a 0.06 shot
        # called 0.20 shows +14 points, a 0.80 favourite called 0.88 only +8 --
        # so the cheapest, least reliably estimated side always won. Requiring
        # edge RELATIVE to price, plus a floor on how cheap a value bet may be,
        # removes that bias.
        self.min_relative_edge = e.get("min_relative_edge", 0.25)
        self.min_value_price = e.get("min_value_price", 0.15)
        self.kelly_fraction = e.get("kelly_fraction", 0.25)
        self.min_confidence = e.get("min_confidence", 0.5)
        self.scorecard = scorecard
        self.risk = risk
        self.research = research

    def decide(self, event: TradeEvent, portfolio: PaperPortfolio,
               current_prices: dict[str, float] | None = None,
               estimate: ProbabilityEstimate | None = None) -> Signal | None:
        return self.evaluate(event, portfolio, current_prices, estimate).signal

    def evaluate(self, event: TradeEvent, portfolio: PaperPortfolio,
                 current_prices: dict[str, float] | None = None,
                 estimate: ProbabilityEstimate | None = None) -> Evaluation:
        importance = min(100.0, event.usd_size / self.whale_usd_threshold * 25) if self.whale_usd_threshold > 0 else 0.0
        accuracy = self.scorecard.accuracy(event.wallet)
        track_record_count = self.scorecard.resolved_count(event.wallet)

        def _eval(*, outcome, price, action, reason, possibility, signal) -> Evaluation:
            return Evaluation(
                wallet=event.wallet, market_id=event.market_id, title=event.title, outcome=outcome,
                price=price, usd_size=event.usd_size, timestamp=event.timestamp, accuracy=accuracy,
                track_record_count=track_record_count, action=action, reason=reason,
                possibility_pct=possibility, importance_pct=importance, signal=signal,
            )

        if event.usd_size < self.whale_usd_threshold:
            return _eval(outcome=event.outcome, price=event.price, action="ignored_too_small",
                          reason=f"Only ${event.usd_size:,.0f}, below the ${self.whale_usd_threshold:,.0f} threshold",
                          possibility=None, signal=None)

        if estimate is None:
            return _eval(outcome=event.outcome, price=event.price, action="skipped_no_research",
                          reason="No outside odds available for this market, so there's nothing to price it against",
                          possibility=None, signal=None)

        if estimate.confidence < self.min_confidence:
            return _eval(outcome=event.outcome, price=event.price, action="skipped_low_confidence",
                          reason=f"Only {estimate.confidence:.0%} confident in the {estimate.probability:.0%} estimate ({estimate.source})",
                          possibility=round(estimate.probability * 100, 1), signal=None)

        # The research prices the traded outcome; the other side is its complement.
        fair_yes = estimate.probability
        opp_price = event.opposite_price if event.opposite_price is not None else max(0.01, min(0.99, 1 - event.price))
        candidates = [
            (event.token_id, event.outcome, event.price, fair_yes),
            (event.opposite_token_id, event.opposite_outcome, opp_price, 1 - fair_yes),
        ]

        # Ranked by edge relative to price rather than raw points, so a cheap
        # longshot no longer automatically outranks a well-priced favourite.
        best = None
        for token_id, outcome, price, fair in candidates:
            if price <= 0 or price >= 1:
                continue
            relative = (fair - price) / price
            if best is None or relative > best[5]:
                best = (token_id, outcome, price, fair, fair - price, relative)

        if best is None:
            return _eval(outcome=event.outcome, price=event.price, action="skipped_no_edge",
                          reason="No usable price on either side of this market",
                          possibility=round(fair_yes * 100, 1), signal=None)

        token_id, outcome, price, fair, edge, relative = best
        possibility = round(fair * 100, 1)
        market_says = f"market {price:.0%} vs research {fair:.0%} ({estimate.source})"

        if edge < self.min_edge or relative < self.min_relative_edge:
            return _eval(outcome=outcome, price=price, action="skipped_no_edge",
                          reason=(f"Priced about right -- {market_says}, {edge * 100:+.1f} points "
                                  f"({relative:+.0%} relative), below the {self.min_edge * 100:.0f}pt / "
                                  f"{self.min_relative_edge:.0%} bar"),
                          possibility=possibility, signal=None)

        floor = max(self.min_entry_price, self.min_value_price)
        if not (floor <= price <= self.max_entry_price):
            return _eval(outcome=outcome, price=price, action="skipped_price_bounds",
                          reason=(f"{market_says}, but {price:.3f} is outside the {floor:g}-{self.max_entry_price:g} "
                                  f"range value bets are allowed in -- estimates are least reliable at the tails"),
                          possibility=possibility, signal=None)

        if self.risk.already_in_market(portfolio, event.market_id):
            return _eval(outcome=outcome, price=price, action="skipped_already_in",
                          reason=f"{market_says}, but this bot already holds a position in this market",
                          possibility=possibility, signal=None)

        if not self.risk.can_open_new_position(portfolio):
            return _eval(outcome=outcome, price=price, action="skipped_full",
                          reason=f"{market_says}, but already at the maximum number of open bets",
                          possibility=possibility, signal=None)

        # Kelly for a $1-payout contract, scaled down because `fair` is an
        # estimate rather than a known truth.
        kelly = edge / (1 - price)
        confidence_scaled = kelly * self.kelly_fraction * estimate.confidence
        amount = self.risk.position_size_usd(
            portfolio=portfolio, market_id=event.market_id,
            confidence=confidence_scaled, current_prices=current_prices,
        )
        if amount <= 0:
            return _eval(outcome=outcome, price=price, action="skipped_capital",
                          reason=f"{market_says}, but risk limits or available cash blocked it",
                          possibility=possibility, signal=None)

        signal = Signal(stance="value", token_id=token_id, outcome=outcome, price=price,
                        usd_amount=amount, wallet=event.wallet)
        return _eval(outcome=outcome, price=price, action="value",
                      reason=f"Underpriced -- {market_says}, {edge * 100:+.1f} points of edge",
                      possibility=possibility, signal=signal)
