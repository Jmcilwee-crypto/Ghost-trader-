"""Statistics across every variant in an experiment -- and across separate
experiment runs, so past results stay comparable instead of being replaced.

    python3 -m src.stats                 # current run
    python3 -m src.stats --all-runs      # current run + every archived run
    python3 -m src.stats --json          # machine-readable, for further analysis

Nothing here writes to the live state files; archived runs are read-only too.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import yaml

from .portfolio import PaperPortfolio


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation coefficient, or None when it isn't defined (fewer than two
    points, or one of the series is completely flat)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denom = math.sqrt(sum(d * d for d in dx)) * math.sqrt(sum(d * d for d in dy))
    if denom == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / denom


def variant_stats(name: str, settings: dict, portfolio: PaperPortfolio) -> dict[str, Any]:
    closed = portfolio.closed_trades
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    staked = sum(t.shares * t.avg_price for t in closed)
    equity = portfolio.equity()

    by_stance: dict[str, dict[str, Any]] = {}
    for t in closed:
        bucket = by_stance.setdefault(t.stance, {"count": 0, "wins": 0, "pnl": 0.0})
        bucket["count"] += 1
        bucket["wins"] += 1 if t.pnl > 0 else 0
        bucket["pnl"] += t.pnl
    for bucket in by_stance.values():
        bucket["win_rate"] = bucket["wins"] / bucket["count"] if bucket["count"] else None
        bucket["pnl"] = round(bucket["pnl"], 2)

    open_prices = [p.avg_price for p in portfolio.positions.values()]
    closed_prices = [t.avg_price for t in closed]
    all_prices = open_prices + closed_prices

    return {
        "name": name,
        "profile": settings.get("profile", "?"),
        "whale_threshold": settings.get("whale_usd_threshold"),
        "min_track_record": settings.get("min_track_record"),
        "equity": round(equity, 2),
        "return_pct": round((equity / portfolio.starting_cash - 1) * 100, 2),
        "cash": round(portfolio.cash, 2),
        "open_count": len(portfolio.positions),
        "open_exposure": round(sum(p.cost_basis for p in portfolio.positions.values()), 2),
        "closed_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "realized_pnl": round(portfolio.realized_pnl(), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        # >1 means winning bets brought in more than losing ones cost.
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        # Average profit per dollar staked on a finished bet.
        "expectancy_per_dollar": round(portfolio.realized_pnl() / staked, 4) if staked else None,
        "avg_entry_price": round(sum(all_prices) / len(all_prices), 3) if all_prices else None,
        "max_drawdown_pct": round(portfolio.max_drawdown() * 100, 2),
        "by_stance": by_stance,
    }


def _scored_wallets(scorecard: dict | None) -> int:
    """The scorecard gained a {wallets, pending} wrapper; counting the raw
    dict would always report 2 instead of the real number of traders."""
    if not scorecard:
        return 0
    if "wallets" in scorecard or "pending" in scorecard:
        return len(scorecard.get("wallets") or {})
    return len(scorecard)   # legacy flat format


def load_run(state_path: Path) -> dict[str, Any]:
    data = json.loads(state_path.read_text())
    variants = data.get("variants", {})
    rows = [
        variant_stats(name, v.get("settings", {}), PaperPortfolio.from_dict(v["portfolio"]))
        for name, v in variants.items()
    ]
    rows.sort(key=lambda r: (-r["equity"], -r["closed_count"], r["name"]))
    return {
        "run": state_path.stem,
        "path": str(state_path),
        "variants": rows,
        "scored_wallets": _scored_wallets(data.get("scorecard")),
    }


def group_summary(rows: list[dict], key: str) -> list[dict]:
    groups: dict[Any, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get(key), []).append(row)

    out = []
    for value, members in groups.items():
        closed = sum(m["closed_count"] for m in members)
        wins = sum(m["wins"] for m in members)
        out.append({
            key: value,
            "variants": len(members),
            "mean_return_pct": round(sum(m["return_pct"] for m in members) / len(members), 2),
            "mean_equity": round(sum(m["equity"] for m in members) / len(members), 2),
            "total_closed": closed,
            "total_open": sum(m["open_count"] for m in members),
            "win_rate": round(wins / closed, 3) if closed else None,
            "total_realized_pnl": round(sum(m["realized_pnl"] for m in members), 2),
        })
    out.sort(key=lambda g: -g["mean_return_pct"])
    return out


def correlations(rows: list[dict]) -> dict[str, float | None]:
    """How each tunable axis relates to performance. Needs finished bets to
    mean anything -- with everything still unresolved these are all None or
    noise."""
    thresholds = [r["whale_threshold"] for r in rows if r["whale_threshold"] is not None]
    returns = [r["return_pct"] for r in rows if r["whale_threshold"] is not None]
    track = [r["min_track_record"] for r in rows if r.get("min_track_record") is not None]
    track_returns = [r["return_pct"] for r in rows if r.get("min_track_record") is not None]
    bets = [r["closed_count"] for r in rows]
    bet_returns = [r["return_pct"] for r in rows]

    return {
        "threshold_vs_return": pearson(thresholds, returns),
        "min_track_record_vs_return": pearson(track, track_returns),
        "bets_finished_vs_return": pearson(bets, bet_returns),
    }


def discover_runs(config: dict, include_archived: bool) -> list[Path]:
    exp_cfg = config.get("experiment", {})
    current = Path(exp_cfg.get("state_path", "data/experiment_state.json"))
    runs = [current] if current.exists() else []
    if include_archived:
        archive_dir = Path(exp_cfg.get("archive_dir", "data/archive"))
        archived = sorted(archive_dir.glob("experiment_state*.json")) if archive_dir.exists() else []
        # Older snapshots were kept beside the live state file; keep reading
        # those so past runs stay in the comparison.
        archived += sorted(current.parent.glob("experiment_state.*.json"))
        runs.extend(p for p in archived if p != current)
    return runs


def _fmt(value, suffix="", width=0, pct=False):
    if value is None:
        return "n/a".rjust(width)
    if pct:
        return f"{value * 100:.1f}%".rjust(width)
    return f"{value}{suffix}".rjust(width)


def print_run(run: dict, top: int | None = None) -> None:
    rows = run["variants"]
    shown = rows if top is None else rows[:top]
    print(f"\n=== {run['run']} ===  ({len(rows)} variants, {run['scored_wallets']} traders scored)")
    print(f"{'#':<3}{'variant':<22}{'equity':>10}{'return':>9}{'open':>6}{'done':>6}{'W/L':>8}{'win%':>7}{'profit factor':>15}{'avg entry':>11}{'max DD':>8}")
    for i, r in enumerate(shown, 1):
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"{i:<3}{r['name']:<22}{r['equity']:>10,.2f}{r['return_pct']:>8.2f}%"
            f"{r['open_count']:>6}{r['closed_count']:>6}{wl:>8}"
            f"{_fmt(r['win_rate'], width=7, pct=True)}"
            f"{_fmt(r['profit_factor'], width=15)}"
            f"{_fmt(r['avg_entry_price'], width=11)}"
            f"{r['max_drawdown_pct']:>7.1f}%"
        )

    for key, label in (("profile", "strategy style"), ("whale_threshold", "minimum bet size")):
        print(f"\n-- grouped by {label} --")
        print(f"{label:<20}{'variants':>10}{'mean return':>14}{'open':>7}{'done':>6}{'win%':>8}{'realized P&L':>15}")
        for g in group_summary(rows, key):
            value = g[key]
            value = f"${value:,.0f}" if key == "whale_threshold" and value is not None else str(value)
            print(
                f"{value:<20}{g['variants']:>10}{g['mean_return_pct']:>13.2f}%"
                f"{g['total_open']:>7}{g['total_closed']:>6}"
                f"{_fmt(g['win_rate'], width=8, pct=True)}{g['total_realized_pnl']:>15,.2f}"
            )

    stance_totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        for stance, bucket in r["by_stance"].items():
            agg = stance_totals.setdefault(stance, {"count": 0, "wins": 0, "pnl": 0.0})
            agg["count"] += bucket["count"]
            agg["wins"] += bucket["wins"]
            agg["pnl"] += bucket["pnl"]
    if stance_totals:
        print("\n-- finished bets by reason for opening (all variants) --")
        print(f"{'stance':<14}{'bets':>7}{'wins':>7}{'win%':>8}{'P&L':>12}")
        for stance, agg in sorted(stance_totals.items(), key=lambda kv: -kv[1]["count"]):
            wr = agg["wins"] / agg["count"] if agg["count"] else None
            print(f"{stance:<14}{agg['count']:>7}{agg['wins']:>7}{_fmt(wr, width=8, pct=True)}{agg['pnl']:>12,.2f}")

    total_closed = sum(r["closed_count"] for r in rows)
    print("\n-- correlations (strategy setting vs return) --")
    if total_closed == 0:
        print("  no finished bets yet -- correlations need resolved markets to mean anything")
    for label, value in correlations(rows).items():
        print(f"  {label:<28}{_fmt(round(value, 3) if value is not None else None, width=8)}")


def compare_runs(runs: list[dict]) -> None:
    if len(runs) < 2:
        return
    print("\n=== same variant across runs ===")
    names = sorted({r["name"] for run in runs for r in run["variants"]})
    print(f"{'variant':<22}" + "".join(f"{run['run'][:18]:>20}" for run in runs))
    print(f"{'':<22}" + "".join(f"{'return (finished)':>20}" for _ in runs))
    for name in names:
        line = f"{name:<22}"
        for run in runs:
            row = next((r for r in run["variants"] if r["name"] == name), None)
            cell = f"{row['return_pct']:+.2f}% ({row['closed_count']})" if row else "n/a"
            line += f"{cell:>20}"
        print(line)


def build_report(config_path: str = "config.yaml", include_archived: bool = False) -> dict[str, Any]:
    config = yaml.safe_load(Path(config_path).read_text())
    paths = discover_runs(config, include_archived)
    return {"runs": [load_run(p) for p in paths]}


def main():
    parser = argparse.ArgumentParser(description="Statistics for every strategy variant in the experiment.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--all-runs", action="store_true", help="include archived runs from previous experiments")
    parser.add_argument("--top", type=int, default=None, help="only show the top N variants per run")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of tables")
    args = parser.parse_args()

    report = build_report(args.config, args.all_runs)
    if not report["runs"]:
        print("No experiment state found -- run `python3 -m src.experiment` first.")
        return

    if args.json:
        print(json.dumps(report, indent=2))
        return

    for run in report["runs"]:
        print_run(run, args.top)
    compare_runs(report["runs"])


if __name__ == "__main__":
    main()
