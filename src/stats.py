"""Statistics across every variant in an experiment -- and across separate
experiment runs, so past results stay comparable instead of being replaced.

    python3 -m src.stats                 # current run
    python3 -m src.stats --all-runs      # current run + every archived run
    python3 -m src.stats --json          # machine-readable, for further analysis

Nothing here writes to the live state files; archived runs are read-only too.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import time
from pathlib import Path
from typing import Any

import yaml

from .portfolio import PaperPortfolio


# -- regimes ----------------------------------------------------------------
# How much of the feed the fleet could see has changed twice, and pooling
# across those changes compares different experiments. The A/B report already
# refuses to print a number for arms that didn't start together; this is the
# same guard applied to the fleet's own history.
#
# Coverage figures use the feed rate measured on 4 Nov 2026 (~1,150 trades per
# minute). Before 22 Oct a cycle read only the newest 100 trades, so at ~5
# scheduled runs a day the fleet saw well under 1% of the day's trading.
REGIMES = [
    (calendar.timegm((2026, 10, 22, 0, 0, 0)), "newest page only",
     "one 100-trade page per run; <1% of the feed seen"),
    (calendar.timegm((2026, 11, 2, 18, 37, 0)), "feed paging",
     "walks back to the last trade processed; ~6% of the feed seen"),
    (calendar.timegm((2026, 11, 5, 0, 0, 0)), "outage",
     "Data API v1 retired; the Polymarket arm recorded nothing"),
    (float("inf"), "data API v2",
     "cursor paging on v2; ~18 min of feed per cycle"),
]


def regime_of(ts: float) -> tuple[str, str]:
    """Which coverage regime a timestamp falls in."""
    for boundary, name, detail in REGIMES:
        if ts < boundary:
            return name, detail
    return REGIMES[-1][1], REGIMES[-1][2]


def regime_split(portfolio: PaperPortfolio) -> dict[str, dict[str, Any]]:
    """Settled bets bucketed by the regime they were opened in."""
    out: dict[str, dict[str, Any]] = {}
    for t in portfolio.closed_trades:
        name, _ = regime_of(t.opened_at)
        row = out.setdefault(name, {"bets": 0, "wins": 0, "pnl": 0.0, "entry": 0.0})
        row["bets"] += 1
        row["wins"] += 1 if t.pnl > 0 else 0
        row["pnl"] += t.pnl
        row["entry"] += t.avg_price
    for row in out.values():
        row["win_rate"] = row["wins"] / row["bets"]
        row["avg_entry"] = row["entry"] / row["bets"]
        del row["entry"]
    return out


def print_regimes(runs: list[dict]) -> None:
    state = runs[0] if runs else None
    if not state:
        return
    print("\n-- settled bets by coverage regime --")
    print("   Pooling across these compares different experiments, not different strategies:")
    print("   the fleet's opportunity set changed, so win rates either side are not like for like.\n")
    totals: dict[str, dict[str, Any]] = {}
    for row in state["variants"]:
        for name, bucket in (row.get("regimes") or {}).items():
            agg = totals.setdefault(name, {"bets": 0, "wins": 0, "pnl": 0.0, "entry": 0.0})
            agg["bets"] += bucket["bets"]
            agg["wins"] += bucket["wins"]
            agg["pnl"] += bucket["pnl"]
            agg["entry"] += bucket["avg_entry"] * bucket["bets"]
    order = [name for _, name, _ in REGIMES]
    print(f"{'regime':<18}{'bets':>8}{'win%':>8}{'avg entry':>11}{'edge':>9}{'P&L':>12}   what changed")
    for name in order:
        agg = totals.get(name)
        detail = next(d for _, n, d in REGIMES if n == name)
        if not agg or not agg["bets"]:
            print(f"{name:<18}{'-':>8}{'-':>8}{'-':>11}{'-':>9}{'-':>12}   {detail}")
            continue
        win = agg["wins"] / agg["bets"]
        entry = agg["entry"] / agg["bets"]
        print(f"{name:<18}{agg['bets']:>8}{win * 100:>7.1f}%{entry:>11.3f}"
              f"{win - entry:>+9.3f}{agg['pnl']:>12,.2f}   {detail}")
    print("\n   (bets are counted once per variant, so these are correlated, not independent)")


# -- capacity ---------------------------------------------------------------
DENIAL_LABELS = [("no_slot", "no slot"), ("no_cash", "no cash"), ("declined", "declined")]


def print_capacity(runs: list[dict]) -> None:
    state = runs[0] if runs else None
    if not state:
        return
    since = state.get("capacity_counting_since")
    totals: dict[str, dict[str, int]] = {}
    for row in state["variants"]:
        for stance, counts in (row.get("capacity") or {}).items():
            agg = totals.setdefault(stance, {})
            for key, value in counts.items():
                agg[key] = agg.get(key, 0) + value

    print("\n-- opportunities by arm, and why they were refused --")
    if not totals:
        print("   No capacity counts recorded yet -- they start accumulating on the next cycle.")
        return
    if since:
        print(f"   Counting since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(since))}.")
    print("   'no slot' is the position cap, not the strategy declining: those are bets an arm")
    print("   wanted and could not place, and they are not in its win rate either way.\n")
    print(f"{'arm':<10}{'wanted':>9}{'taken':>8}{'no slot':>10}{'no cash':>9}"
          f"{'declined':>10}{'denied by capacity':>21}")
    for stance in ("copy", "fade", "explore", "value"):
        agg = totals.get(stance)
        if not agg:
            continue
        taken = agg.get("taken", 0)
        no_slot = agg.get("no_slot", 0)
        no_cash = agg.get("no_cash", 0)
        declined = agg.get("declined", 0)
        wanted = taken + no_slot + no_cash
        rate = (no_slot + no_cash) / wanted if wanted else 0.0
        print(f"{stance:<10}{wanted:>9,}{taken:>8,}{no_slot:>10,}{no_cash:>9,}"
              f"{declined:>10,}{rate * 100:>20.1f}%")
    starved = [st for st in ("copy", "fade", "explore", "value")
               if (totals.get(st) or {}).get("no_slot", 0) + (totals.get(st) or {}).get("no_cash", 0)
               > (totals.get(st) or {}).get("taken", 0)]
    if starved:
        print(f"\n   {', '.join(starved)} was refused more often than it was filled. Comparing its")
        print("   returns against an arm that wasn't is comparing capacity, not skill.")


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


def variant_stats(name: str, settings: dict, portfolio: PaperPortfolio,
                  capacity: dict | None = None) -> dict[str, Any]:
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
        "capacity": capacity or {},
        "regimes": regime_split(portfolio),
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
        variant_stats(name, v.get("settings", {}), PaperPortfolio.from_dict(v["portfolio"]),
                      v.get("capacity"))
        for name, v in variants.items()
    ]
    rows.sort(key=lambda r: (-r["equity"], -r["closed_count"], r["name"]))
    return {
        "run": state_path.stem,
        "path": str(state_path),
        "variants": rows,
        "scored_wallets": _scored_wallets(data.get("scorecard")),
        "capacity_counting_since": data.get("capacity_counting_since"),
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


def ab_report(state_path: str = "data/experiment_state.json") -> list[dict]:
    """Pair up the flat-stake A/B arms and report each side by side.

    Reports the two arms' first-bet times as well as their returns, because
    the first version of this experiment was invalidated by exactly that gap:
    the flat arms were added 12.5 days late, so the comparison measured a head
    start rather than a sizing rule. A pair whose arms did not start together
    is not evidence, and this says so rather than printing a tidy number.
    """
    import json as _json

    raw = _json.loads(Path(state_path).read_text())
    variants = raw.get("variants", raw)

    pairs: dict[str, dict] = {}
    for name, entry in variants.items():
        settings = entry.get("settings") or {}
        pair_id = settings.get("ab_pair")
        if not pair_id:
            continue
        portfolio = entry.get("portfolio") or {}
        closed = portfolio.get("closed_trades") or []
        start = portfolio.get("starting_cash") or 1000.0
        equity = portfolio.get("cash", start) + sum(
            p.get("shares", 0) * p.get("avg_price", 0)
            for p in (portfolio.get("positions") or {}).values()
        )
        pairs.setdefault(pair_id, {})[settings.get("ab_arm", "?")] = {
            "name": name,
            "return_pct": (equity - start) / start * 100 if start else 0.0,
            "realized_pnl": sum(c.get("pnl", 0.0) for c in closed),
            "settled": len(closed),
            "first_bet_at": min((c.get("opened_at") for c in closed), default=None),
            "avg_stake": (sum(c.get("avg_price", 0) * c.get("shares", 0) for c in closed) / len(closed)
                          if closed else 0.0),
        }

    out = []
    for pair_id, arms in sorted(pairs.items()):
        scaled, flat = arms.get("scaled"), arms.get("flat")
        row = {"pair": pair_id, "scaled": scaled, "flat": flat, "valid": False, "note": ""}
        if not scaled or not flat:
            row["note"] = "incomplete pair -- one arm is missing"
        elif not scaled["settled"] or not flat["settled"]:
            row["note"] = "not enough data yet -- one arm has settled nothing"
        else:
            gap_hours = abs(scaled["first_bet_at"] - flat["first_bet_at"]) / 3600.0
            row["start_gap_hours"] = gap_hours
            if gap_hours > 24:
                row["note"] = (f"CONFOUNDED: arms started {gap_hours / 24:.1f} days apart, "
                               "so this measures a head start, not the sizing rule")
            else:
                row["valid"] = True
                row["diff_pp"] = flat["return_pct"] - scaled["return_pct"]
                row["note"] = f"flat {'ahead' if row['diff_pp'] > 0 else 'behind'} by {abs(row['diff_pp']):.2f}pp"
        out.append(row)
    return out


def print_ab(rows: list[dict]) -> None:
    if not rows:
        print("No A/B cohort in this run.")
        return
    print("=== flat-stake A/B ===")
    print(f"{'pair':24} {'scaled':>9} {'flat':>9} {'diff':>8} {'settled':>12}")
    for row in rows:
        s, f = row.get("scaled"), row.get("flat")
        if s and f:
            diff = f"{row['diff_pp']:+.2f}pp" if row.get("valid") else "--"
            print(f"{row['pair']:24} {s['return_pct']:+8.2f}% {f['return_pct']:+8.2f}% "
                  f"{diff:>8} {s['settled']:5}/{f['settled']:<6}")
        else:
            print(f"{row['pair']:24} {'(incomplete)':>40}")
        print(f"{'':24} {row['note']}")
    valid = [r for r in rows if r.get("valid")]
    if valid:
        ahead = sum(1 for r in valid if r["diff_pp"] > 0)
        print(f"\n  flat ahead in {ahead} of {len(valid)} valid pair(s); "
              f"{len(rows) - len(valid)} pair(s) not yet usable")
    else:
        print("\n  no pair is usable evidence yet")


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
    parser.add_argument("--ab", action="store_true", help="report the flat-stake A/B cohort only")
    parser.add_argument("--caps", action="store_true",
                        help="report how many opportunities each arm was denied for lack of room")
    parser.add_argument("--regimes", action="store_true",
                        help="split settled bets by coverage regime instead of pooling them")
    args = parser.parse_args()

    if args.ab:
        print_ab(ab_report())
        return

    report = build_report(args.config, args.all_runs)
    if not report["runs"]:
        print("No experiment state found -- run `python3 -m src.experiment` first.")
        return

    if args.json:
        print(json.dumps(report, indent=2))
        return

    if args.caps:
        print_capacity(report["runs"])
        return
    if args.regimes:
        print_regimes(report["runs"])
        return

    for run in report["runs"]:
        print_run(run, args.top)
    compare_runs(report["runs"])


if __name__ == "__main__":
    main()
