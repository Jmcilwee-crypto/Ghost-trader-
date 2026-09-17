"""Runs many strategy variants side by side against ONE shared live data feed.

Why not just run N copies of live.py: each copy would independently poll the
same public endpoints, multiplying API load by N and blowing through
Polymarket's rate limit (~60 req/min) -- and worse, each copy would see
slightly different trade timing, so any performance difference between them
would be partly noise rather than strategy.

Here a single poller fetches each trade once, resolves each market's metadata
once, and feeds the identical TradeEvent to every variant. The variants share
a scorecard too (it records observed public outcomes, not decisions), so they
differ ONLY in their decision rules -- which is what makes the comparison a
fair test rather than 20 loosely-related runs.
"""
from __future__ import annotations

import argparse
import copy
import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .market_utils import is_binary_market, parse_timestamp, winning_token_id
from .polymarket_client import PolymarketClient
from .portfolio import PaperPortfolio
from .research import build_research
from .risk import RiskConfig, RiskManager
from .scorecard import TraderScorecard
from .strategy.base import TradeEvent
from .strategy.edge_value import EdgeValueStrategy
from .strategy.whale_follow import WhaleFollowStrategy

_STOP = False


def _handle_sigint(signum, frame):
    global _STOP
    _STOP = True
    print("\nStop requested, finishing this cycle and saving state...")


# Four decision profiles x five whale thresholds = 20 variants.
PROFILES = {
    "aggressive": {"copy_above_accuracy": 0.55, "fade_below_accuracy": 0.45, "min_track_record": 3, "explore_unknown_wallets": True},
    "balanced": {"copy_above_accuracy": 0.60, "fade_below_accuracy": 0.40, "min_track_record": 5, "explore_unknown_wallets": True},
    "strict": {"copy_above_accuracy": 0.70, "fade_below_accuracy": 0.30, "min_track_record": 10, "explore_unknown_wallets": True},
    # fade_below of -1 can never trigger, so this profile only ever copies.
    "copy_only": {"copy_above_accuracy": 0.60, "fade_below_accuracy": -1.0, "min_track_record": 5, "explore_unknown_wallets": False},
}
WHALE_THRESHOLDS = [500.0, 1000.0, 2500.0, 5000.0, 10000.0]

# Research-driven variants, added alongside the whale bots so the experiment
# measures whether outside odds actually beat copying traders. Each is a
# minimum edge: how far below fair value the price must be before betting.
EDGE_VARIANTS = [
    {"name": "value-edge5", "min_edge": 0.05, "kelly_fraction": 0.25},
    {"name": "value-edge10", "min_edge": 0.10, "kelly_fraction": 0.25},
    {"name": "value-edge15", "min_edge": 0.15, "kelly_fraction": 0.25},
    {"name": "value-edge10-bold", "min_edge": 0.10, "kelly_fraction": 0.50},
]


@dataclass
class Variant:
    name: str
    strategy: WhaleFollowStrategy
    portfolio: PaperPortfolio
    settings: dict


def build_variants(base_config: dict, scorecard: TraderScorecard, research=None) -> list[Variant]:
    variants: list[Variant] = []
    risk = RiskManager(RiskConfig(**base_config["risk"]))
    starting_cash = base_config["bankroll"]["starting_usd"]

    for profile_name, profile in PROFILES.items():
        for threshold in WHALE_THRESHOLDS:
            config = copy.deepcopy(base_config)
            config["strategy"].update(profile)
            config["strategy"]["whale_usd_threshold"] = threshold
            name = f"{profile_name}@${int(threshold)}"
            variants.append(Variant(
                name=name,
                strategy=WhaleFollowStrategy(config=config, scorecard=scorecard, risk=risk),
                portfolio=PaperPortfolio(starting_cash),
                settings={"profile": profile_name, "whale_usd_threshold": threshold, **profile},
            ))

    edge_cfg = base_config.get("edge_strategy", {})
    if research is not None and getattr(research, "enabled", False) and edge_cfg.get("enabled", True):
        edge_threshold = edge_cfg.get("whale_usd_threshold", 500.0)
        for spec in EDGE_VARIANTS:
            config = copy.deepcopy(base_config)
            config["strategy"]["whale_usd_threshold"] = edge_threshold
            config.setdefault("edge_strategy", {})
            config["edge_strategy"] = {**config["edge_strategy"], **spec}
            variants.append(Variant(
                name=spec["name"],
                strategy=EdgeValueStrategy(config=config, scorecard=scorecard, risk=risk, research=research),
                portfolio=PaperPortfolio(starting_cash),
                settings={"profile": "value", "whale_usd_threshold": edge_threshold,
                          "min_edge": spec["min_edge"], "kelly_fraction": spec["kelly_fraction"]},
            ))
    return variants


def archive_existing_state(state_path: str, archive_dir: str) -> str | None:
    """Snapshot the previous run's state before this one starts writing over
    it. Past runs are never destroyed -- they're the only way to compare a
    strategy change against what it replaced."""
    src = Path(state_path)
    if not src.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(src.stat().st_mtime))
    dest = Path(archive_dir) / f"experiment_state_{stamp}.json"
    if dest.exists():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return str(dest)


def append_history(path: str, rows: list[dict]) -> None:
    """One line per cycle holding every variant's equity, so performance over
    time can be reconstructed later rather than only its latest snapshot."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "ts": time.time(),
        "variants": {
            r["name"]: {"equity": round(r["equity"], 2), "open": r["open_positions"], "closed": r["closed_trades"]}
            for r in rows
        },
    }
    with open(p, "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def _log_trimmed(path: str, entry: dict, max_entries: int) -> None:
    """Append a row, keeping the file capped to the most recent max_entries."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), **entry}
    lines = p.read_text().splitlines() if p.exists() else []
    lines.append(json.dumps(entry))
    if len(lines) > max_entries:
        lines = lines[-max_entries:]
    p.write_text("\n".join(lines) + "\n")


def save_state(path: str, variants: list[Variant], scorecard: TraderScorecard, last_seen_ts: float,
               seen_hashes: set[str], research_stats: dict | None = None) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({
        "variants": {v.name: {"settings": v.settings, "portfolio": v.portfolio.to_dict()} for v in variants},
        "scorecard": scorecard.to_dict(),
        "research": research_stats or {},
        "last_seen_ts": last_seen_ts,
        # Capped: this is only used to avoid double-processing trades across
        # restarts, and an unbounded list would bloat the state file forever.
        "seen_hashes": list(seen_hashes)[-5000:],
    }, indent=2))


def load_state(path: str, variants: list[Variant], scorecard: TraderScorecard) -> tuple[float, set[str]]:
    p = Path(path)
    if not p.exists():
        return 0.0, set()
    data = json.loads(p.read_text())
    scorecard.load_dict(data.get("scorecard", {}))
    saved = data.get("variants", {})
    for v in variants:
        if v.name in saved:
            v.portfolio = PaperPortfolio.from_dict(saved[v.name]["portfolio"])
    return data.get("last_seen_ts", 0.0), set(data.get("seen_hashes", []))


def leaderboard(variants: list[Variant]) -> list[dict]:
    rows = []
    for v in variants:
        equity = v.portfolio.equity()
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


def print_leaderboard(rows: list[dict], top: int = 5) -> None:
    print(f"\n{'variant':<24}{'equity':>12}{'return':>10}{'open':>7}{'closed':>8}{'win rate':>10}")
    for row in rows[:top]:
        win = f"{row['win_rate']:.0%}" if row["win_rate"] is not None else "n/a"
        print(f"{row['name']:<24}{row['equity']:>12,.2f}{row['return_pct']:>9.1f}%{row['open_positions']:>7}{row['closed_trades']:>8}{win:>10}")


def run_experiment(config_path: str = "config.yaml", once: bool = False) -> int:
    signal.signal(signal.SIGINT, _handle_sigint)

    config = yaml.safe_load(Path(config_path).read_text())
    exp_cfg = config.get("experiment", {})
    live_cfg = config["live"]
    state_path = exp_cfg.get("state_path", "data/experiment_state.json")
    leaderboard_path = exp_cfg.get("leaderboard_path", "data/experiment_leaderboard.json")
    poll_interval = exp_cfg.get("poll_interval_seconds", live_cfg["poll_interval_seconds"])
    resolution_interval = exp_cfg.get("resolution_check_interval_seconds", live_cfg.get("resolution_check_interval_seconds", 300))

    flagged_log_path = exp_cfg.get("flagged_log_path", "data/experiment_flagged_log.jsonl")
    flagged_log_max = exp_cfg.get("flagged_log_max_entries", live_cfg.get("flagged_log_max_entries", 300))

    archive_dir = exp_cfg.get("archive_dir", "data/archive")
    history_path = exp_cfg.get("history_path", "data/experiment_history.jsonl")
    # See spot_experiment: git history replaces per-run archives when scheduled.
    archived = None if once else archive_existing_state(state_path, archive_dir)
    if archived:
        print(f"Previous run archived to {archived}")

    client = PolymarketClient()
    scorecard = TraderScorecard()
    research = build_research(config)
    variants = build_variants(config, scorecard, research)
    last_seen_ts, seen_hashes = load_state(state_path, variants, scorecard)
    if getattr(research, "enabled", False):
        print(f"Research enabled: {research.name}")
    else:
        print("Research disabled (set ODDS_API_KEY and research.enabled to add value-betting bots)")

    # Flagged rows are rated from one variant's point of view so the numbers
    # in that panel stay consistent; the rest of the row is variant-agnostic.
    reference_name = exp_cfg.get("reference_variant", "balanced@$1000")
    reference_variant = next((v for v in variants if v.name == reference_name), variants[0])
    reference_evaluation = None

    # Trades below every variant's threshold can't affect any of them, so we
    # skip the market-metadata lookup for those entirely -- that lookup is the
    # single biggest source of API calls in this loop.
    min_threshold = min(v.strategy.whale_usd_threshold for v in variants)

    market_cache: dict[str, dict] = {}
    last_resolution_check: dict[str, float] = {}

    print(f"Running {len(variants)} variants, each starting at ${config['bankroll']['starting_usd']:,.2f}")
    print(f"Whale thresholds: {WHALE_THRESHOLDS} x profiles: {list(PROFILES)}")

    while not _STOP:
        try:
            trades = client.get_trades(limit=live_cfg["trades_lookback_limit"])
        except RuntimeError as exc:
            print(f"trade fetch failed: {exc}", file=sys.stderr)
            if once:
                # `continue` would jump past the once-check at the end of
                # the loop and retry forever. Under a scheduler the next
                # run IS the retry, so report the dud cycle and get out.
                print("no data this cycle -- exiting for the scheduler to retry",
                      file=sys.stderr)
                return 1
            time.sleep(poll_interval)
            continue

        new_trades = [t for t in trades if t.get("transactionHash") not in seen_hashes]
        new_trades.sort(key=lambda t: parse_timestamp(t.get("timestamp")) or 0)

        for t in new_trades:
            ts = parse_timestamp(t.get("timestamp"))
            if ts is None or ts < last_seen_ts - 3600:
                continue

            condition_id = t.get("conditionId") or t.get("market")
            token_id = t.get("asset")
            wallet = t.get("proxyWallet")
            price = _safe_float(t.get("price"))
            size = _safe_float(t.get("size"))
            tx_hash = t.get("transactionHash")
            if tx_hash:
                seen_hashes.add(tx_hash)
            if not condition_id or not token_id or not wallet or price <= 0 or price >= 1:
                continue

            usd_size = size * price
            if usd_size < min_threshold:
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

            scorecard.observe_trade(wallet=wallet, market_id=condition_id, token_id=token_id, usd_size=usd_size, price=price)

            event = TradeEvent(
                wallet=wallet, market_id=condition_id, title=market.get("question", condition_id),
                token_id=token_id, outcome=outcome_by_token[token_id], price=price, usd_size=usd_size,
                timestamp=ts, opposite_token_id=other_token, opposite_outcome=outcome_by_token[other_token],
            )

            # Researched once per trade and shared by every variant, so adding
            # value bots doesn't multiply the outside API calls.
            estimate = research.estimate(event.title, event.outcome)

            acted_count = 0
            for v in variants:
                evaluation = v.strategy.evaluate(event, v.portfolio, None, estimate)
                if v is reference_variant:
                    reference_evaluation = evaluation
                sig = evaluation.signal
                if sig is None:
                    continue
                try:
                    v.portfolio.open_or_add(
                        market_id=event.market_id, token_id=sig.token_id, outcome=sig.outcome,
                        title=event.title, price=sig.price, usd_amount=sig.usd_amount,
                        timestamp=ts, stance=sig.stance,
                    )
                    acted_count += 1
                except ValueError:
                    pass

            # One flagged-log row per trade (not per variant), rated from the
            # reference variant, plus how many of the variants acted on it.
            if reference_evaluation.action != "ignored_too_small":
                _log_trimmed(flagged_log_path, {
                    "wallet": wallet, "market": event.title, "outcome": reference_evaluation.outcome,
                    "price": reference_evaluation.price, "usd_size": event.usd_size,
                    "accuracy": reference_evaluation.accuracy, "track_record_count": reference_evaluation.track_record_count,
                    "action": reference_evaluation.action, "reason": reference_evaluation.reason,
                    "possibility_pct": reference_evaluation.possibility_pct,
                    "importance_pct": reference_evaluation.importance_pct,
                    "acted": acted_count > 0, "variants_acted": acted_count, "variants_total": len(variants),
                }, flagged_log_max)

            last_seen_ts = max(last_seen_ts, ts)

        # Resolution check, done ONCE per market and applied to every variant.
        now = time.time()
        open_market_ids = {p.market_id for v in variants for p in v.portfolio.positions.values()}
        for market_id in open_market_ids:
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
            if not market or not market.get("closed"):
                continue
            market_cache[market_id] = market
            ok, token_ids, _ = is_binary_market(market)
            if not ok:
                continue
            win_token = winning_token_id(market, token_ids)
            if win_token is None:
                continue

            scorecard.resolve_market(market_id, winning_token_id=win_token)
            for v in variants:
                for tok in [tk for tk, p in v.portfolio.positions.items() if p.market_id == market_id]:
                    v.portfolio.resolve_position(tok, won=tok == win_token, timestamp=now)

        for v in variants:
            v.portfolio.record_equity(now)

        rows = leaderboard(variants)
        save_state(state_path, variants, scorecard, last_seen_ts, seen_hashes, research.stats())
        Path(leaderboard_path).write_text(json.dumps(rows, indent=2))
        append_history(history_path, rows)
        print_leaderboard(rows)

        if once:
            # Single-cycle mode: the schedule is the loop.
            break

        for _ in range(poll_interval):
            if _STOP:
                break
            time.sleep(1)

    save_state(state_path, variants, scorecard, last_seen_ts, seen_hashes, research.stats())
    print("State saved. Bye.")
    return 0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main():
    parser = argparse.ArgumentParser(description="Run many strategy variants against one shared live feed.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (for scheduled runners like cron/CI)")
    args = parser.parse_args()
    raise SystemExit(run_experiment(args.config, once=args.once))


if __name__ == "__main__":
    main()
