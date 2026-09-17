"""Turns the on-disk experiment state into one plain dict that's easy to
render, whether that's the web dashboard, a CLI, or tests. Pure read -- never
writes anything.

The dashboard is experiment-first: the headline numbers describe the
best-performing variant, the leaderboard ranks all of them, and the
position/P&L detail tables drill into whichever variant is currently leading.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .portfolio import PaperPortfolio
from .spot_portfolio import SpotPortfolio
from .stats import variant_stats


def _rating_label(pct: float | None) -> str:
    if pct is None:
        return "Unknown"
    if pct >= 75:
        return "Very High"
    if pct >= 50:
        return "High"
    if pct >= 25:
        return "Medium"
    return "Low"


def _position_row(p, live_prices: dict, is_spot: bool) -> dict:
    row = {
        "title": p.title,
        "outcome": p.outcome,
        "stance": p.stance,
        "usd_in": round(p.cost_basis, 2),
        "shares": round(p.shares, 8 if is_spot else 2),
        "avg_price": round(p.avg_price, 2 if is_spot else 4),
        "opened_at": p.opened_at,
    }
    if is_spot:
        price = live_prices.get(p.market_id)
        row["current_price"] = round(price, 2) if price else None
        # Spot positions have no ceiling, so what matters is where it stands now.
        row["unrealized_pnl"] = round(p.shares * price - p.cost_basis, 2) if price else None
        row["unrealized_pct"] = round((price / p.avg_price - 1) * 100, 2) if price and p.avg_price else None
        row["max_gain_pct"] = None
    else:
        # Best case if it wins: outcome tokens always redeem at $1.
        row["max_gain_pct"] = round((1 - p.avg_price) / p.avg_price * 100, 1) if p.avg_price > 0 else None
    return row


def _scored_wallets(scorecard: dict | None) -> int:
    """Count wallets with a track record. The scorecard gained a {wallets,
    pending} wrapper; counting the raw dict would report 2 regardless of how
    many traders had actually been scored."""
    if not scorecard:
        return 0
    if "wallets" in scorecard or "pending" in scorecard:
        return len(scorecard.get("wallets") or {})
    return len(scorecard)   # legacy flat format


def _market_list(config: dict | None = None) -> list[dict]:
    """Every market plus a one-line health read, so all experiments are
    visible on the switcher without having to toggle between them."""
    if config is None:
        return [{"key": "polymarket", "label": "Polymarket"}]
    out = []
    for key, spec in markets_from_config(config).items():
        entry = {"key": key, "label": spec["label"]}
        entry.update(_quick_summary(config, key, spec))
        out.append(entry)
    return out


def _quick_summary(config: dict, market: str, spec: dict) -> dict:
    path = Path(spec["default_state"])
    if not path.exists():
        return {"has_data": False}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"has_data": False}

    variants = data.get("variants", {})
    if not variants:
        return {"has_data": False}

    is_spot = spec.get("is_spot", False)
    prices = data.get("prices", {}) if is_spot else {}
    loader = SpotPortfolio.from_dict if is_spot else PaperPortfolio.from_dict
    best, open_count, closed_count = None, 0, 0
    for v in variants.values():
        try:
            p = loader(v["portfolio"])
        except (KeyError, TypeError):
            continue
        equity = p.equity(prices)
        best = equity if best is None else max(best, equity)
        open_count += len(p.positions)
        closed_count += len(p.closed_trades)

    starting = config["bankroll"]["starting_usd"]
    return {
        "has_data": True,
        "bots": len(variants),
        "best_return_pct": round((best / starting - 1) * 100, 2) if best and starting else 0.0,
        "open": open_count,
        "finished": closed_count,
    }


def _read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _equity_series(history_path: Path, points: int = 120) -> dict[str, list[list[float]]]:
    """Per-variant [[timestamp, equity], ...] for the sparklines, thinned to
    at most `points` samples so a long-running experiment doesn't ship a
    megabyte of JSON to the browser on every refresh."""
    snapshots = _read_jsonl_tail(history_path, 5000)
    if not snapshots:
        return {}
    step = max(1, len(snapshots) // points)
    sampled = snapshots[::step]
    if snapshots and sampled[-1] is not snapshots[-1]:
        sampled.append(snapshots[-1])  # always keep the latest value

    series: dict[str, list[list[float]]] = {}
    for snap in sampled:
        ts = snap.get("ts")
        for name, entry in snap.get("variants", {}).items():
            series.setdefault(name, []).append([ts, entry.get("equity")])
    return series


POLYMARKET = {
    "label": "Polymarket",
    "config_key": "experiment",
    "default_state": "data/experiment_state.json",
    "default_history": "data/experiment_history.jsonl",
    "default_flagged": "data/experiment_flagged_log.jsonl",
    "runner": "src.experiment",
    "is_spot": False,
}


def markets_from_config(config: dict) -> dict[str, dict]:
    """Every tradable experiment the dashboard can show. Spot asset classes are
    read straight from config, so adding one there makes it appear here without
    touching this module."""
    pm = dict(POLYMARKET)
    exp = config.get("experiment", {})
    pm["default_state"] = exp.get("state_path", pm["default_state"])
    pm["default_history"] = exp.get("history_path", pm["default_history"])
    pm["default_flagged"] = exp.get("flagged_log_path", pm["default_flagged"])
    markets = {"polymarket": pm}
    spot = config.get("spot", {})
    for key, spec in (spot.get("classes") or {}).items():
        markets[key] = {
            "label": spec.get("label", key.title()),
            "config_key": "spot",
            "asset_class": key,
            "default_state": spec.get("state_path", f"data/spot_{key}_state.json"),
            "default_history": spec.get("history_path", f"data/spot_{key}_history.jsonl"),
            "default_flagged": spec.get("flagged_log_path", f"data/spot_{key}_flagged_log.jsonl"),
            "runner": f"src.spot_experiment --market {key}",
            "is_spot": True,
        }
    return markets


def build_dashboard_data(config_path: str = "config.yaml", market: str = "polymarket") -> dict[str, Any]:
    config = yaml.safe_load(Path(config_path).read_text())
    registry = markets_from_config(config)
    spec = registry.get(market) or registry["polymarket"]
    is_spot = spec.get("is_spot", False)

    exp_cfg = config.get(spec["config_key"], {})
    dash_cfg = config.get("dashboard", {})
    target = config["bankroll"]["target_usd"]
    ruin = config["bankroll"]["ruin_usd"]
    starting = config["bankroll"]["starting_usd"]

    state_path = Path(spec["default_state"])
    if not state_path.exists():
        return {
            "has_data": False, "market": market, "markets": _market_list(config),
            "message": f"No {spec['label']} data yet -- run `python3 -m {spec['runner']}` in a terminal "
                        f"(leave it running), then refresh this page.",
        }

    data = json.loads(state_path.read_text())
    saved_variants = data.get("variants", {})
    if not saved_variants:
        return {"has_data": False, "market": market, "markets": _market_list(config),
                "message": "State file is empty -- give the runner a cycle or two, then refresh."}

    loader = SpotPortfolio.from_dict if is_spot else PaperPortfolio.from_dict
    portfolios = {name: loader(v["portfolio"]) for name, v in saved_variants.items()}
    # Spot positions float with the market, so they must be marked to the last
    # seen price; binary contracts are carried at cost until they settle.
    live_prices = data.get("prices", {}) if is_spot else {}

    history_path = Path(spec["default_history"])
    series = _equity_series(history_path)
    closed_limit = dash_cfg.get("max_closed_trades_shown", 50)

    leaderboard = []
    for name, portfolio in portfolios.items():
        settings = saved_variants[name].get("settings", {})
        row = variant_stats(name, settings, portfolio)
        # Passed through wholesale rather than cherry-picked: whale bots and
        # value bots are tuned by different knobs, and hardcoding one set
        # silently drops the other's.
        row["settings"] = dict(settings)
        row["equity_series"] = series.get(name, [])
        if is_spot:
            # Recompute equity against live prices -- variant_stats carried the
            # position at cost, which understates or overstates an open trade.
            row["equity"] = round(portfolio.equity(live_prices), 2)
            row["return_pct"] = round((row["equity"] / portfolio.starting_cash - 1) * 100, 2)
            row["unrealized_pnl"] = round(portfolio.unrealized_pnl(live_prices), 2)
            row["fees_paid"] = round(getattr(portfolio, "fees_paid", 0.0), 2)
        row["positions"] = [
            _position_row(p, live_prices, is_spot)
            for p in sorted(portfolio.positions.values(), key=lambda p: -p.cost_basis)
        ]
        row["closed"] = [
            {
                "title": t.title,
                "outcome": t.outcome,
                "stance": t.stance,
                "won": t.pnl > 0,
                "pnl": round(t.pnl, 2),
                "usd_in": round(t.shares * t.avg_price, 2),
                "avg_price": round(t.avg_price, 4),
                "closed_at": t.closed_at,
            }
            for t in sorted(portfolio.closed_trades, key=lambda t: -t.closed_at)[:closed_limit]
        ]
        leaderboard.append(row)
    # Equity alone ties at the starting bankroll before anything resolves, so
    # break ties by how much each variant has actually done -- otherwise the
    # same arbitrary handful always sits at the top.
    leaderboard.sort(key=lambda r: (-r["equity"], -r["closed_count"], -r["open_count"], r["name"]))

    leader_name = leaderboard[0]["name"]
    leader = portfolios[leader_name]
    leader_equity = leader.equity(live_prices)

    flagged_limit = dash_cfg.get("max_flagged_shown", 50)
    flagged_path = Path(spec["default_flagged"])
    flagged = [
        {
            **entry,
            "importance_label": _rating_label(entry.get("importance_pct")),
            "possibility_label": _rating_label(entry.get("possibility_pct")),
        }
        for entry in reversed(_read_jsonl_tail(flagged_path, flagged_limit))  # most recent first
    ]

    total_closed = sum(len(p.closed_trades) for p in portfolios.values())
    all_closed = [t for p in portfolios.values() for t in p.closed_trades]
    overall_win_rate = (sum(1 for t in all_closed if t.pnl > 0) / len(all_closed)) if all_closed else None
    progress_pct = max(0.0, min(100.0, (leader_equity - starting) / (target - starting) * 100)) if target != starting else 0.0

    summary = {
        "starting_usd": starting,
        "leader_name": leader_name,
        "equity": round(leader_equity, 2),
        "cash": round(leader.cash, 2),
        "invested": round(leader_equity - leader.cash, 2),
        "change_usd": round(leader_equity - starting, 2),
        "change_pct": round((leader_equity / starting - 1) * 100, 2) if starting else 0.0,
        "target_usd": target,
        "ruin_usd": ruin,
        "progress_pct": round(progress_pct, 1),
        "realized_pnl": round(leader.realized_pnl(), 2),
        "win_rate": leader.win_rate(),
        "max_drawdown_pct": round(leader.max_drawdown() * 100, 1),
        "open_count": len(leader.positions),
        "closed_count": len(leader.closed_trades),
        "known_wallets": _scored_wallets(data.get("scorecard")),
        "variant_count": len(leaderboard),
        "variants_trading": sum(1 for r in leaderboard if r["open_count"] or r["closed_count"]),
        "total_closed_all_variants": total_closed,
        "overall_win_rate": overall_win_rate,
        "total_open_all_variants": sum(r["open_count"] for r in leaderboard),
        "total_exposure_all_variants": round(sum(r["open_exposure"] for r in leaderboard), 2),
        "research": data.get("research", {}),
        "is_spot": is_spot,
        "prices": live_prices,
    }

    return {
        "has_data": True,
        "market": market,
        "market_label": spec["label"],
        "markets": _market_list(config),
        "is_spot": is_spot,
        "summary": summary,
        "leaderboard": leaderboard,
        "flagged": flagged,
    }
