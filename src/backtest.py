"""Replays the whale copy/fade strategy over recently-closed Polymarket
markets to sanity-check it before ever running it live.

Approach (kept deliberately simple, see README for caveats):
  1. Pull the N highest-volume closed markets from Gamma.
  2. Pull each market's trade history from the Data API.
  3. Merge everything into one chronological stream of trade + resolution
     events and replay it through PaperPortfolio / TraderScorecard /
     WhaleFollowStrategy in time order, so a wallet's track record only ever
     reflects markets that had *already* resolved at that point in time.

This is a rough backtest, not a rigorous one: Polymarket's trade endpoint is
paginated per-market and rate-limited, so market/trade coverage is partial,
and there's no slippage/fee modeling. Treat the output as a plausibility
check, not a performance guarantee.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import yaml

from .market_utils import is_binary_market, parse_timestamp, winning_token_id
from .polymarket_client import PolymarketClient
from .portfolio import PaperPortfolio
from .risk import RiskConfig, RiskManager
from .scorecard import TraderScorecard
from .strategy.base import TradeEvent
from .strategy.whale_follow import WhaleFollowStrategy


def load_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def gather_events(config: dict, client: PolymarketClient) -> list[dict]:
    bt = config["backtest"]
    markets = client.list_closed_markets(limit=bt["num_closed_markets"], min_volume=bt["min_market_volume_usd"])
    print(f"Fetched {len(markets)} closed markets (volume >= ${bt['min_market_volume_usd']:,.0f})")

    events: list[dict] = []
    used_markets = 0
    for market in markets:
        ok, token_ids, outcomes = is_binary_market(market)
        if not ok:
            continue
        win_token = winning_token_id(market, token_ids)
        if win_token is None:
            continue
        resolve_ts = parse_timestamp(market.get("closedTime") or market.get("endDate"))
        if resolve_ts is None:
            continue

        condition_id = market.get("conditionId")
        title = market.get("question", market.get("slug", condition_id))
        if not condition_id:
            continue

        try:
            trades = client.get_trades(market=condition_id, limit=bt["trades_per_market_limit"])
        except RuntimeError as exc:
            print(f"  skip {title!r}: trade fetch failed ({exc})", file=sys.stderr)
            continue
        if not trades:
            continue

        outcome_by_token = dict(zip(token_ids, outcomes))
        used_markets += 1

        for t in trades:
            ts = parse_timestamp(t.get("timestamp"))
            token_id = t.get("asset")
            wallet = t.get("proxyWallet")
            price = _safe_float(t.get("price"))
            size = _safe_float(t.get("size"))
            if ts is None or token_id not in outcome_by_token or not wallet or price <= 0 or price >= 1:
                continue
            other_token = token_ids[0] if token_id == token_ids[1] else token_ids[1]
            events.append({
                "type": "trade",
                "ts": ts,
                "wallet": wallet,
                "market_id": condition_id,
                "title": title,
                "token_id": token_id,
                "outcome": outcome_by_token[token_id],
                "opposite_token_id": other_token,
                "opposite_outcome": outcome_by_token[other_token],
                "price": price,
                "usd_size": size * price,
            })

        events.append({
            "type": "resolve",
            "ts": resolve_ts,
            "market_id": condition_id,
            "winning_token_id": win_token,
        })

    print(f"Using {used_markets} binary, cleanly-resolved markets; {sum(1 for e in events if e['type']=='trade')} trades total")
    events.sort(key=lambda e: e["ts"])
    return events


def run_backtest(config_path: str = "config.yaml") -> dict:
    config = load_config(config_path)
    client = PolymarketClient()
    events = gather_events(config, client)
    if not events:
        raise RuntimeError("No usable historical events fetched -- check network access to Polymarket APIs.")

    portfolio = PaperPortfolio(starting_cash=config["bankroll"]["starting_usd"])
    scorecard = TraderScorecard()
    risk = RiskManager(RiskConfig(**config["risk"]))
    strategy = WhaleFollowStrategy(config=config, scorecard=scorecard, risk=risk)

    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]
    stop_reason = None
    stop_ts = None

    for event in events:
        if event["type"] == "trade":
            te = TradeEvent(
                wallet=event["wallet"], market_id=event["market_id"], title=event["title"],
                token_id=event["token_id"], outcome=event["outcome"], price=event["price"],
                usd_size=event["usd_size"], timestamp=event["ts"],
                opposite_token_id=event["opposite_token_id"], opposite_outcome=event["opposite_outcome"],
            )
            scorecard.observe_trade(wallet=te.wallet, market_id=te.market_id, token_id=te.token_id, usd_size=te.usd_size, price=te.price)

            signal = strategy.decide(te, portfolio)
            if signal is not None:
                portfolio.open_or_add(
                    market_id=te.market_id, token_id=signal.token_id, outcome=signal.outcome, title=te.title,
                    price=signal.price, usd_amount=signal.usd_amount, timestamp=te.timestamp, stance=signal.stance,
                )
        else:  # resolve
            scorecard.resolve_market(event["market_id"], winning_token_id=event["winning_token_id"])
            for token_id in [t for t, p in portfolio.positions.items() if p.market_id == event["market_id"]]:
                won = token_id == event["winning_token_id"]
                portfolio.resolve_position(token_id, won=won, timestamp=event["ts"])

        portfolio.record_equity(event["ts"])
        stop = portfolio.check_stop_condition(target=target, ruin=ruin)
        if stop and stop_reason is None:
            stop_reason = stop
            stop_ts = event["ts"]

    report = {
        "starting_cash": portfolio.starting_cash,
        "final_equity": portfolio.equity(),
        "return_pct": (portfolio.equity() / portfolio.starting_cash - 1) * 100,
        "realized_pnl": portfolio.realized_pnl(),
        "num_closed_trades": len(portfolio.closed_trades),
        "num_open_positions": len(portfolio.positions),
        "win_rate": portfolio.win_rate(),
        "max_drawdown_pct": portfolio.max_drawdown() * 100,
        "target_usd": target,
        "ruin_usd": ruin,
        "stop_reason": stop_reason,
        "stop_ts": stop_ts,
    }

    bt = config["backtest"]
    Path(bt["report_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(bt["report_path"]).write_text(json.dumps(report, indent=2))
    with open(bt["equity_curve_csv"], "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "equity"])
        writer.writerows(portfolio.equity_curve)

    return report


def main():
    parser = argparse.ArgumentParser(description="Backtest the whale copy/fade strategy on closed Polymarket markets.")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    report = run_backtest(args.config)
    print("\n=== Backtest report ===")
    for k, v in report.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
