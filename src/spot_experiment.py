"""Runs many spot-trading strategies side by side against one shared price feed.

Same architecture as the Polymarket experiment and for the same reason: one
poller fetches each symbol's candles once per cycle and every variant sees
identical bars, so a difference in results is a difference in strategy rather
than a difference in luck or timing.

What's different from the prediction-market side is the instrument. There's no
settlement here -- a position ends only when the strategy sells it -- so every
cycle marks open positions to the live price, and fees are charged on both
sides of the round trip.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .market_data import build_source
from .research import load_env_file
from .risk import RiskConfig, RiskManager
from .spot_portfolio import SpotPortfolio
from .strategy.spot import (BreakoutStrategy, BuyAndHoldStrategy, CatalystStrategy,
                             MeanReversionStrategy, MomentumStrategy, VolatilityScaledMomentum)

_STOP = False


def _handle_sigint(signum, frame):
    global _STOP
    _STOP = True
    print("\nStop requested, finishing this cycle and saving state...")


# Each entry becomes one bot. Momentum and mean reversion are deliberate
# opposites; buy_and_hold is the control every other bot has to beat.
SPOT_VARIANTS = [
    {"name": "momentum-5/20", "cls": MomentumStrategy, "params": {"fast": 5, "slow": 20, "stop_loss_pct": 0.10}},
    {"name": "momentum-10/50", "cls": MomentumStrategy, "params": {"fast": 10, "slow": 50, "stop_loss_pct": 0.10}},
    {"name": "momentum-20/100", "cls": MomentumStrategy, "params": {"fast": 20, "slow": 100, "stop_loss_pct": 0.15}},
    {"name": "meanrev-25", "cls": MeanReversionStrategy, "params": {"oversold": 25, "exit_level": 55, "stop_loss_pct": 0.10}},
    {"name": "meanrev-30", "cls": MeanReversionStrategy, "params": {"oversold": 30, "exit_level": 55, "stop_loss_pct": 0.10}},
    {"name": "meanrev-35", "cls": MeanReversionStrategy, "params": {"oversold": 35, "exit_level": 60, "stop_loss_pct": 0.10}},
    {"name": "breakout-20", "cls": BreakoutStrategy, "params": {"lookback": 20, "exit_lookback": 10, "stop_loss_pct": 0.12}},
    {"name": "breakout-50", "cls": BreakoutStrategy, "params": {"lookback": 50, "exit_lookback": 20, "stop_loss_pct": 0.12}},
    {"name": "vol-momentum", "cls": VolatilityScaledMomentum, "params": {"fast": 10, "slow": 50, "stop_loss_pct": 0.10}},
    {"name": "momentum-tp", "cls": MomentumStrategy, "params": {"fast": 10, "slow": 50, "stop_loss_pct": 0.08, "take_profit_pct": 0.15}},
    {"name": "buy-and-hold", "cls": BuyAndHoldStrategy, "params": {}},
]


# A deliberately riskier tier, opt-in per asset class. Forex and equities move
# far less per bar than crypto, so the standard bots barely trade them -- these
# use shorter lookbacks (more signals), much larger stakes, and looser stops.
# They are expected to be more volatile; that is the point of running them
# next to the conservative set rather than instead of it.
RISKY_SPOT_VARIANTS = [
    {"name": "risky-momentum-3/10", "cls": MomentumStrategy,
     "params": {"fast": 3, "slow": 10, "stop_loss_pct": 0.06},
     "risk": {"max_pct_per_trade": 0.15}},
    {"name": "risky-momentum-5/15", "cls": MomentumStrategy,
     "params": {"fast": 5, "slow": 15, "stop_loss_pct": 0.08, "take_profit_pct": 0.10},
     "risk": {"max_pct_per_trade": 0.20}},
    {"name": "risky-meanrev-20", "cls": MeanReversionStrategy,
     "params": {"oversold": 20, "exit_level": 50, "stop_loss_pct": 0.08},
     "risk": {"max_pct_per_trade": 0.20}},
    {"name": "risky-breakout-10", "cls": BreakoutStrategy,
     "params": {"lookback": 10, "exit_lookback": 5, "stop_loss_pct": 0.07},
     "risk": {"max_pct_per_trade": 0.15}},
    {"name": "risky-nostop-5/20", "cls": MomentumStrategy,
     # No stop at all: tests whether cutting losses helps or just books them.
     "params": {"fast": 5, "slow": 20},
     "risk": {"max_pct_per_trade": 0.25, "max_pct_per_market": 0.30}},
]


@dataclass
class SpotVariant:
    name: str
    strategy: object
    portfolio: SpotPortfolio
    settings: dict
    risk: RiskManager | None = None


def _variant_risk(config: dict, overrides: dict | None) -> RiskManager | None:
    if not overrides:
        return None
    merged = {**config["risk"], **overrides}
    return RiskManager(RiskConfig(**merged))


def build_spot_variants(config: dict, fee_rate: float | None = None,
                        include_risky: bool = False, catalysts: list[dict] | None = None) -> list[SpotVariant]:
    starting_cash = config["bankroll"]["starting_usd"]
    if fee_rate is None:
        fee_rate = config.get("spot", {}).get("fee_rate", 0.001)

    specs = list(SPOT_VARIANTS)
    if include_risky:
        specs += RISKY_SPOT_VARIANTS

    variants = []
    for spec in specs:
        variants.append(SpotVariant(
            name=spec["name"],
            strategy=spec["cls"](**spec["params"]),
            portfolio=SpotPortfolio(starting_cash, fee_rate=fee_rate),
            settings={"profile": spec["cls"].name, **spec["params"], **(spec.get("risk") or {})},
            risk=_variant_risk(config, spec.get("risk")),
        ))

    for cat in (catalysts or []):
        for label, exit_days in (("hold-through", 0), ("sell-into", cat.get("exit_days_before", 3))):
            name = f"catalyst-{cat['symbol'].lower()}-{label}"
            variants.append(SpotVariant(
                name=name,
                strategy=CatalystStrategy(
                    symbol=cat["symbol"], event_date=cat["date"],
                    event_name=cat.get("name", "the event"),
                    enter_days_before=cat.get("enter_days_before", 60),
                    exit_days_before=exit_days,
                    tranches=cat.get("tranches", 4),
                    stop_loss_pct=cat.get("stop_loss_pct", 0.12),
                ),
                portfolio=SpotPortfolio(starting_cash, fee_rate=fee_rate),
                settings={"profile": "catalyst", "symbol": cat["symbol"], "event": cat.get("name"),
                          "event_date": cat["date"], "exit_days_before": exit_days},
                risk=_variant_risk(config, cat.get("risk")),
            ))
    return variants


def leaderboard(variants: list[SpotVariant], prices: dict[str, float]) -> list[dict]:
    rows = []
    for v in variants:
        equity = v.portfolio.equity(prices)
        rows.append({
            "name": v.name,
            "equity": equity,
            "return_pct": (equity / v.portfolio.starting_cash - 1) * 100,
            "cash": v.portfolio.cash,
            "open_positions": len(v.portfolio.positions),
            "closed_trades": len(v.portfolio.closed_trades),
            "win_rate": v.portfolio.win_rate(),
            "realized_pnl": v.portfolio.realized_pnl(),
            "settings": v.settings,
        })
    rows.sort(key=lambda r: -r["equity"])
    return rows


def save_state(path: str, variants: list[SpotVariant], prices: dict[str, float], data_stats: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({
        "variants": {v.name: {"settings": v.settings, "portfolio": v.portfolio.to_dict()} for v in variants},
        "prices": prices,
        "research": data_stats,
        "scorecard": {},
        "last_seen_ts": time.time(),
        "seen_hashes": [],
    }, indent=2))


def load_state(path: str, variants: list[SpotVariant]) -> None:
    p = Path(path)
    if not p.exists():
        return
    saved = json.loads(p.read_text()).get("variants", {})
    for v in variants:
        if v.name in saved:
            v.portfolio = SpotPortfolio.from_dict(saved[v.name]["portfolio"])


def archive_existing_state(state_path: str, archive_dir: str, asset_class: str = "spot") -> str | None:
    src = Path(state_path)
    if not src.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(src.stat().st_mtime))
    dest = Path(archive_dir) / f"spot_{asset_class}_state_{stamp}.json"
    if dest.exists():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return str(dest)


def append_history(path: str, rows: list[dict]) -> None:
    """One line per cycle with every bot's equity, so the dashboard can draw
    trend sparklines rather than only the latest snapshot."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "ts": time.time(),
        "variants": {r["name"]: {"equity": round(r["equity"], 2), "open": r["open_positions"],
                                  "closed": r["closed_trades"]} for r in rows},
    }
    with open(p, "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def _log_trimmed(path: str, entry: dict, max_entries: int) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), **entry}
    lines = p.read_text().splitlines() if p.exists() else []
    lines.append(json.dumps(entry))
    if len(lines) > max_entries:
        lines = lines[-max_entries:]
    p.write_text("\n".join(lines) + "\n")


def class_config(config: dict, asset_class: str) -> dict:
    """Merge the shared spot defaults with one asset class's overrides, and
    derive its file paths so each class keeps its own isolated state."""
    spot_cfg = dict(config.get("spot", {}))
    classes = spot_cfg.pop("classes", {})
    if asset_class not in classes:
        raise SystemExit(f"Unknown asset class {asset_class!r}. Available: {', '.join(classes) or 'none'}")
    merged = {**spot_cfg, **classes[asset_class]}
    merged.setdefault("label", asset_class.title())
    merged.setdefault("state_path", f"data/spot_{asset_class}_state.json")
    merged.setdefault("history_path", f"data/spot_{asset_class}_history.jsonl")
    merged.setdefault("flagged_log_path", f"data/spot_{asset_class}_flagged_log.jsonl")
    return merged


def run_spot_experiment(config_path: str = "config.yaml", asset_class: str = "crypto",
                        once: bool = False) -> int:
    signal.signal(signal.SIGINT, _handle_sigint)
    load_env_file()   # API keys live in the gitignored .env, not config.yaml

    config = yaml.safe_load(Path(config_path).read_text())
    spot_cfg = class_config(config, asset_class)
    symbols = spot_cfg.get("symbols", [])
    poll_interval = spot_cfg.get("poll_interval_seconds", 120)
    state_path = spot_cfg["state_path"]
    flagged_path = spot_cfg["flagged_log_path"]
    flagged_max = spot_cfg.get("flagged_log_max_entries", 300)
    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]

    # Skipped in --once mode: a scheduled runner commits state every cycle, so
    # version control already is the archive and snapshotting each run would
    # add a file every few minutes.
    archived = None if once else archive_existing_state(
        state_path, spot_cfg.get("archive_dir", "data/archive"), asset_class)
    if archived:
        print(f"Previous run archived to {archived}")

    data = build_source(spot_cfg)
    if not getattr(data, "enabled", True):
        raise SystemExit(
            f"{spot_cfg['label']} data source is not configured. "
            f"{data.stats().get('note') or 'Missing API key.'}"
        )
    risk = RiskManager(RiskConfig(**config["risk"]))
    variants = build_spot_variants(
        config,
        fee_rate=spot_cfg.get("fee_rate", 0.001),
        include_risky=spot_cfg.get("include_risky", False),
        catalysts=spot_cfg.get("catalysts"),
    )
    load_state(state_path, variants)

    print(f"Running {len(variants)} {spot_cfg['label']} bots on {', '.join(symbols)} "
          f"({spot_cfg.get('interval')} bars via {data.name}, "
          f"${config['bankroll']['starting_usd']:,.0f} each)")

    while not _STOP:
        prices: dict[str, float] = {}
        history: dict[str, list[float]] = {}
        for symbol in symbols:
            closes = data.closes(symbol)
            if closes:
                history[symbol] = closes
                prices[symbol] = closes[-1]

        if not prices:
            print("no price data available this cycle", file=sys.stderr)
            if once:
                # See experiment.py: `continue` skips the once-check, so a
                # blocked source would loop until the job timed out.
                print("no data this cycle -- exiting for the scheduler to retry",
                      file=sys.stderr)
                return 1
            time.sleep(poll_interval)
            continue

        for symbol, closes in history.items():
            for v in variants:
                pos = v.portfolio.positions.get(symbol)
                decision = v.strategy.decide(closes, holding=pos is not None,
                                              entry_price=pos.avg_price if pos else None,
                                              symbol=symbol)
                # Risky and catalyst bots carry their own limits; everyone
                # else shares the conservative default.
                v_risk = v.risk or risk
                scaling_in = decision.target_fraction is not None
                if decision.action == "buy" and (pos is None or scaling_in):
                    if pos is None and not v_risk.can_open_new_position(v.portfolio):
                        continue
                    if scaling_in:
                        # Buy only the shortfall against where the ramp says
                        # this position should already be, so repeated cycles
                        # inside one tranche don't keep adding.
                        # Named tranche_target, NOT target: `target` is the
                        # bankroll goal this loop stops on, and shadowing it
                        # here set the stop condition to one tranche's dollar
                        # size -- so the first catalyst buy ended the run.
                        equity = v.portfolio.equity(prices)
                        tranche_target = equity * v_risk.config.max_pct_per_market * decision.target_fraction
                        amount = min(tranche_target - (pos.cost_basis if pos else 0.0), v.portfolio.cash)
                    else:
                        amount = v_risk.position_size_usd(portfolio=v.portfolio, market_id=symbol,
                                                           confidence=decision.strength, current_prices=prices)
                    if amount <= 1:
                        continue
                    try:
                        v.portfolio.buy(symbol=symbol, price=prices[symbol], usd_amount=amount,
                                         timestamp=time.time(), strategy=v.strategy.name)
                        _log_trimmed(flagged_path, {
                            "bot": v.name, "symbol": symbol, "action": "buy", "price": prices[symbol],
                            "usd": round(amount, 2), "reason": decision.reason,
                            "strength_pct": round(decision.strength * 100, 1),
                        }, flagged_max)
                        print(f"[{v.name}] BUY {symbol} @ {prices[symbol]:,.2f} (${amount:,.2f}) -- {decision.reason}")
                    except ValueError:
                        pass
                elif decision.action == "sell" and pos is not None:
                    trade = v.portfolio.sell(symbol=symbol, price=prices[symbol],
                                              timestamp=time.time(), reason=decision.reason)
                    if trade:
                        _log_trimmed(flagged_path, {
                            "bot": v.name, "symbol": symbol, "action": "sell", "price": prices[symbol],
                            "pnl": round(trade.pnl, 2), "reason": decision.reason,
                            "strength_pct": round(decision.strength * 100, 1),
                        }, flagged_max)
                        print(f"[{v.name}] SELL {symbol} @ {prices[symbol]:,.2f} "
                              f"pnl=${trade.pnl:+,.2f} -- {decision.reason}")

        now = time.time()
        for v in variants:
            v.portfolio.record_equity(now, prices)

        rows = leaderboard(variants, prices)
        save_state(state_path, variants, prices, data.stats())
        append_history(spot_cfg["history_path"], rows)

        best = rows[0]
        print(f"{time.strftime('%H:%M:%S')} best={best['name']} ${best['equity']:,.2f} "
              f"({best['return_pct']:+.2f}%) | {sum(r['open_positions'] for r in rows)} positions open")

        if best["equity"] >= target:
            print(f"\nStopping: {best['name']} reached the ${target:,.0f} target.")
            break
        if all(r["equity"] <= ruin for r in rows):
            print("\nStopping: every bot fell below the ruin threshold.")
            break

        if once:
            # Single-cycle mode for scheduled runners (GitHub Actions and the
            # like), where the schedule provides the loop and the process is
            # expected to exit so the job can finish.
            break

        for _ in range(poll_interval):
            if _STOP:
                break
            time.sleep(1)

    save_state(state_path, variants, prices if "prices" in dir() else {}, data.stats())
    print("State saved. Bye.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Run many spot-trading strategies against one shared price feed.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--market", default="crypto",
                        help="asset class from config.yaml spot.classes (crypto, commodities, forex, stocks)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (for scheduled runners like cron/CI)")
    args = parser.parse_args()
    raise SystemExit(run_spot_experiment(args.config, args.market, once=args.once))


if __name__ == "__main__":
    main()
