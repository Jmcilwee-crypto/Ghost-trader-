"""Live paper trading loop: polls Polymarket's public trade feed, applies the
whale copy/fade strategy, and simulates a $1000-starting portfolio. This
NEVER places a real order or touches a real wallet -- it only reads public
data and writes to a local JSON state file + JSONL decision log.

Run it, leave it running (e.g. in tmux/nohup), and it stops itself once
equity hits the configured target or ruin threshold.
"""
from __future__ import annotations

import argparse
import json
import signal
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

_STOP = False


def _handle_sigint(signum, frame):
    global _STOP
    _STOP = True
    print("\nStop requested, finishing this cycle and saving state...")


class LiveState:
    def __init__(self, portfolio: PaperPortfolio, scorecard: TraderScorecard, last_seen_ts: float, seen_hashes: set[str]):
        self.portfolio = portfolio
        self.scorecard = scorecard
        self.last_seen_ts = last_seen_ts
        self.seen_hashes = seen_hashes

    @classmethod
    def fresh(cls, starting_cash: float) -> "LiveState":
        return cls(PaperPortfolio(starting_cash), TraderScorecard(), 0.0, set())

    @classmethod
    def load(cls, path: str, starting_cash: float) -> "LiveState":
        p = Path(path)
        if not p.exists():
            return cls.fresh(starting_cash)
        data = json.loads(p.read_text())
        portfolio = PaperPortfolio.from_dict(data["portfolio"])
        scorecard = TraderScorecard()
        scorecard.load_dict(data["scorecard"])
        return cls(portfolio, scorecard, data["last_seen_ts"], set(data["seen_hashes"]))

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({
            "portfolio": self.portfolio.to_dict(),
            "scorecard": self.scorecard.to_dict(),
            "last_seen_ts": self.last_seen_ts,
            "seen_hashes": list(self.seen_hashes),
        }, indent=2))


def _log(path: str, entry: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), **entry}
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _log_trimmed(path: str, entry: dict, max_entries: int) -> None:
    """Same as _log, but keeps the file capped to the most recent
    max_entries lines so a long-running bot doesn't grow this file forever."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), **entry}
    lines = p.read_text().splitlines() if p.exists() else []
    lines.append(json.dumps(entry))
    if len(lines) > max_entries:
        lines = lines[-max_entries:]
    p.write_text("\n".join(lines) + "\n")


def run_live(config_path: str = "config.yaml") -> None:
    signal.signal(signal.SIGINT, _handle_sigint)

    config = yaml.safe_load(Path(config_path).read_text())
    live_cfg = config["live"]
    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]

    client = PolymarketClient()
    state = LiveState.load(live_cfg["state_path"], config["bankroll"]["starting_usd"])
    risk = RiskManager(RiskConfig(**config["risk"]))
    strategy = WhaleFollowStrategy(config=config, scorecard=state.scorecard, risk=risk)

    market_cache: dict[str, dict] = {}
    last_resolution_check: dict[str, float] = {}
    resolution_interval = live_cfg.get("resolution_check_interval_seconds", 300)
    print(f"Starting equity: ${state.portfolio.equity():,.2f} | target ${target:,.0f} | ruin ${ruin:,.0f}")

    while not _STOP:
        try:
            trades = client.get_trades(limit=live_cfg["trades_lookback_limit"])
        except RuntimeError as exc:
            print(f"trade fetch failed: {exc}", file=sys.stderr)
            time.sleep(live_cfg["poll_interval_seconds"])
            continue

        new_trades = [t for t in trades if t.get("transactionHash") not in state.seen_hashes]
        new_trades.sort(key=lambda t: parse_timestamp(t.get("timestamp")) or 0)

        for t in new_trades:
            ts = parse_timestamp(t.get("timestamp"))
            if ts is None or ts < state.last_seen_ts - 3600:
                continue  # too old / unparsable, ignore

            condition_id = t.get("conditionId") or t.get("market")
            token_id = t.get("asset")
            wallet = t.get("proxyWallet")
            price = _safe_float(t.get("price"))
            size = _safe_float(t.get("size"))
            tx_hash = t.get("transactionHash")
            if tx_hash:
                state.seen_hashes.add(tx_hash)
            if not condition_id or not token_id or not wallet or price <= 0 or price >= 1:
                continue

            market = market_cache.get(condition_id)
            if market is None:
                try:
                    market = client.get_market_by_condition_id(condition_id) or {}
                except RuntimeError:
                    market = {}
                market_cache[condition_id] = market

            ok, token_ids, outcomes = is_binary_market(market)
            if not ok or token_id not in token_ids:
                continue
            outcome_by_token = dict(zip(token_ids, outcomes))
            other_token = token_ids[0] if token_id == token_ids[1] else token_ids[1]

            usd_size = size * price
            state.scorecard.observe_trade(wallet=wallet, market_id=condition_id, token_id=token_id, usd_size=usd_size, price=price)

            event = TradeEvent(
                wallet=wallet, market_id=condition_id, title=market.get("question", condition_id),
                token_id=token_id, outcome=outcome_by_token[token_id], price=price, usd_size=usd_size,
                timestamp=ts, opposite_token_id=other_token, opposite_outcome=outcome_by_token[other_token],
            )
            evaluation = strategy.evaluate(event, state.portfolio)
            if evaluation.action != "ignored_too_small":
                # Log every whale-sized trade we looked at -- acted on or
                # not -- so the dashboard can show what's been flagged.
                _log_trimmed(live_cfg["flagged_log_path"], {
                    "wallet": wallet, "market": event.title, "outcome": evaluation.outcome, "price": evaluation.price,
                    "usd_size": evaluation.usd_size, "accuracy": evaluation.accuracy, "track_record_count": evaluation.track_record_count,
                    "action": evaluation.action, "reason": evaluation.reason,
                    "possibility_pct": evaluation.possibility_pct, "importance_pct": evaluation.importance_pct,
                    "acted": evaluation.signal is not None,
                }, live_cfg["flagged_log_max_entries"])

            signal_ = evaluation.signal
            if signal_ is not None:
                try:
                    pos = state.portfolio.open_or_add(
                        market_id=event.market_id, token_id=signal_.token_id, outcome=signal_.outcome,
                        title=event.title, price=signal_.price, usd_amount=signal_.usd_amount,
                        timestamp=ts, stance=signal_.stance,
                    )
                    _log(live_cfg["log_path"], {
                        "event": "open_position", "stance": signal_.stance, "wallet": wallet,
                        "market": event.title, "outcome": signal_.outcome, "price": signal_.price,
                        "usd_amount": signal_.usd_amount, "trigger_wallet_accuracy": state.scorecard.accuracy(wallet),
                    })
                    print(f"[{signal_.stance}] {event.title[:60]} -> {signal_.outcome} @ {signal_.price:.2f} (${signal_.usd_amount:.2f})")
                except ValueError:
                    pass

            state.last_seen_ts = max(state.last_seen_ts, ts)

        # Check open positions for resolution. Throttled deliberately: doing
        # this for every open market on every poll cycle burns the API rate
        # limit (~60 req/min) as soon as more than a handful of positions are
        # open, and a market can't resolve meaningfully sooner than its endDate.
        now = time.time()
        for market_id in {p.market_id for p in state.portfolio.positions.values()}:
            if now - last_resolution_check.get(market_id, 0.0) < resolution_interval:
                continue
            cached_end = parse_timestamp((market_cache.get(market_id) or {}).get("endDate"))
            if cached_end is not None and now < cached_end - 3600:
                continue
            last_resolution_check[market_id] = now
            try:
                market = client.get_market_by_condition_id(market_id)
            except RuntimeError:
                continue
            if not market:
                continue
            market_cache[market_id] = market
            if not market.get("closed"):
                continue
            ok, token_ids, _ = is_binary_market(market)
            if not ok:
                continue
            win_token = winning_token_id(market, token_ids)
            if win_token is None:
                continue
            state.scorecard.resolve_market(market_id, winning_token_id=win_token)
            for token_id in [t for t, p in state.portfolio.positions.items() if p.market_id == market_id]:
                won = token_id == win_token
                trade = state.portfolio.resolve_position(token_id, won=won, timestamp=time.time())
                if trade:
                    _log(live_cfg["log_path"], {"event": "resolve_position", "market": trade.title, "won": won, "pnl": trade.pnl})
                    print(f"[resolved {'WIN' if won else 'LOSS'}] {trade.title[:60]} pnl=${trade.pnl:+.2f}")

        state.portfolio.record_equity(time.time())
        state.save(live_cfg["state_path"])

        equity = state.portfolio.equity()
        stop = state.portfolio.check_stop_condition(target=target, ruin=ruin)
        print(f"equity=${equity:,.2f} cash=${state.portfolio.cash:,.2f} open_positions={len(state.portfolio.positions)}")
        if stop:
            print(f"\nStopping: hit {stop} condition. Final equity ${equity:,.2f}.")
            break

        for _ in range(live_cfg["poll_interval_seconds"]):
            if _STOP:
                break
            time.sleep(1)

    state.save(live_cfg["state_path"])
    print("State saved. Bye.")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser(description="Run the live (paper-only) whale copy/fade trader.")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    run_live(args.config)


if __name__ == "__main__":
    main()
