"""Read-only snapshot of the live bot's current state -- safe to run in a
second terminal while `python3 -m src.live` keeps running, since it only
reads the JSON files live.py writes, never writes to them itself.

Default output is written in plain English for someone who doesn't know
anything about code or trading jargon. Pass --technical for the dense,
numbers-first version instead.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .portfolio import PaperPortfolio


def _fmt_pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _short_wallet(wallet: str | None) -> str:
    if not wallet:
        return "a trader"
    return f"trader {wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else f"trader {wallet}"


def _when(ts: float | None) -> str:
    if not ts:
        return ""
    delta = datetime.now(timezone.utc).timestamp() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    return f"{int(delta // 86400)} days ago"


def _describe_log_entry(entry: dict) -> str:
    event = entry.get("event")
    when = _when(entry.get("ts"))
    if event == "open_position":
        stance = entry.get("stance")
        who = _short_wallet(entry.get("wallet"))
        market = entry.get("market", "a market")
        outcome = entry.get("outcome", "?")
        amount = _money(entry.get("usd_amount", 0))
        if stance == "copy":
            why = f"because {who} has a good track record"
        elif stance == "fade":
            why = f"betting against {who}, who has a poor track record"
        else:
            why = f"a small test bet since {who} doesn't have a track record yet"
        return f"{when}: put {amount} on \"{outcome}\" for \"{market}\" -- {why}"
    if event == "resolve_position":
        market = entry.get("market", "a market")
        won = entry.get("won")
        pnl = entry.get("pnl", 0)
        if won:
            return f"{when}: WON the bet on \"{market}\" -- gained {_money(pnl)}"
        return f"{when}: LOST the bet on \"{market}\" -- down {_money(abs(pnl))}"
    return f"{when}: {entry}"


def print_status_plain(config_path: str, tail_log: int) -> None:
    config = yaml.safe_load(Path(config_path).read_text())
    state_path = Path(config["live"]["state_path"])
    log_path = Path(config["live"]["log_path"])
    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]
    starting = config["bankroll"]["starting_usd"]

    if not state_path.exists():
        print("The bot hasn't saved any progress yet -- has `python3 -m src.live` run at least once?")
        return

    data = json.loads(state_path.read_text())
    portfolio = PaperPortfolio.from_dict(data["portfolio"])
    equity = portfolio.equity()
    change = equity - starting
    progress = max(0.0, min(100.0, (equity - starting) / (target - starting) * 100)) if target != starting else 0.0

    print("Ghost Trader -- plain-English status")
    print("=" * 40)
    print(f"Started with {_money(starting)} of pretend money.")
    if change >= 0:
        print(f"Right now it's worth {_money(equity)} -- up {_money(change)}.")
    else:
        print(f"Right now it's worth {_money(equity)} -- down {_money(abs(change))}.")
    print(f"Goal: reach {_money(target)}. It's {progress:.0f}% of the way there.")
    print(f"It'll automatically stop if it hits the {_money(target)} goal, or falls to {_money(ruin)} (a safety stop-loss).")
    print(f"Of that {_money(equity)}, {_money(portfolio.cash)} is sitting uninvested and the rest is tied up in active bets.")

    wins = sum(1 for t in portfolio.closed_trades if t.pnl > 0)
    total_closed = len(portfolio.closed_trades)
    if total_closed:
        print(f"\nSo far it has finished {total_closed} bet(s): {wins} won, {total_closed - wins} lost "
              f"({_fmt_pct(portfolio.win_rate())} correct). Money made/lost on finished bets: {_money(portfolio.realized_pnl())}.")
    else:
        print("\nIt hasn't finished (won or lost) any bets yet.")

    if portfolio.positions:
        print(f"\nActive bets right now ({len(portfolio.positions)}):")
        for pos in sorted(portfolio.positions.values(), key=lambda p: -p.cost_basis):
            action = "copying a trader it trusts" if pos.stance == "copy" else \
                      "betting against a trader with a bad track record" if pos.stance == "fade" else \
                      "trying a small test bet"
            print(f"  - \"{pos.title[:70]}\"")
            print(f"      Betting on: {pos.outcome}  |  Put in: {_money(pos.cost_basis)}  |  Why: {action}")
    else:
        print("\nNo active bets right now -- it's waiting for a signal it trusts.")

    if tail_log and log_path.exists():
        lines = log_path.read_text().strip().splitlines()[-tail_log:]
        if lines:
            print(f"\nWhat it's done recently (last {len(lines)}):")
            for line in lines:
                print(f"  - {_describe_log_entry(json.loads(line))}")


def print_status_technical(config_path: str, tail_log: int) -> None:
    config = yaml.safe_load(Path(config_path).read_text())
    state_path = Path(config["live"]["state_path"])
    log_path = Path(config["live"]["log_path"])
    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]
    starting = config["bankroll"]["starting_usd"]

    if not state_path.exists():
        print(f"No state file yet at {state_path} -- has `python3 -m src.live` run at least one cycle?")
        return

    data = json.loads(state_path.read_text())
    portfolio = PaperPortfolio.from_dict(data["portfolio"])
    equity = portfolio.equity()
    progress = (equity - starting) / (target - starting) * 100 if target != starting else 0.0

    print("=== Ghost Trader Status ===")
    print(f"Equity:          ${equity:,.2f}  ({equity / starting - 1:+.1%} since start)")
    print(f"Cash:            ${portfolio.cash:,.2f}")
    print(f"Target/Ruin:     ${target:,.0f} / ${ruin:,.0f}   ({progress:.1f}% of the way to target)")
    print(f"Open positions:  {len(portfolio.positions)}")
    print(f"Closed trades:   {len(portfolio.closed_trades)}  (win rate: {_fmt_pct(portfolio.win_rate())})")
    print(f"Realized P&L:    ${portfolio.realized_pnl():,.2f}")
    print(f"Max drawdown:    {portfolio.max_drawdown():.1%}")
    print(f"Known wallets:   {len(data.get('scorecard', {}))}")

    if portfolio.positions:
        print("\n-- Open positions --")
        for pos in sorted(portfolio.positions.values(), key=lambda p: -p.cost_basis):
            print(f"  [{pos.stance:7s}] {pos.title[:55]:55s} {pos.outcome:5s} {pos.shares:8.1f} sh @ {pos.avg_price:.3f}  (${pos.cost_basis:,.2f})")

    if tail_log and log_path.exists():
        lines = log_path.read_text().strip().splitlines()[-tail_log:]
        if lines:
            print(f"\n-- Last {len(lines)} log entries --")
            for line in lines:
                entry = json.loads(line)
                entry.pop("ts", None)
                print(f"  {entry}")


def main():
    parser = argparse.ArgumentParser(description="Print a snapshot of the live ghost-trading bot's current state.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tail", type=int, default=10, help="number of recent log entries to show (0 to skip)")
    parser.add_argument("--technical", action="store_true", help="show the dense numbers-first view instead of plain English")
    args = parser.parse_args()
    if args.technical:
        print_status_technical(args.config, args.tail)
    else:
        print_status_plain(args.config, args.tail)


if __name__ == "__main__":
    main()
