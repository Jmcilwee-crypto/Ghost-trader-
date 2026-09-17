import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src.portfolio import PaperPortfolio
from src.stats import build_report, correlations, group_summary, load_run, pearson, variant_stats

BASE_CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())


def _portfolio_with(closed):
    """closed: list of (usd_in, won)"""
    p = PaperPortfolio(1000.0)
    for i, (usd, won) in enumerate(closed):
        p.open_or_add(market_id=f"m{i}", token_id=f"t{i}", outcome="Yes", title=f"Market {i}",
                      price=0.5, usd_amount=usd, timestamp=1, stance="copy")
        p.resolve_position(f"t{i}", won=won, timestamp=2)
    return p


def test_pearson_detects_perfect_positive_and_negative_relationships():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_returns_none_when_undefined():
    assert pearson([1], [1]) is None          # too few points
    assert pearson([1, 1, 1], [1, 2, 3]) is None  # flat series has no variance
    assert pearson([1, 2], [1, 2, 3]) is None     # mismatched lengths


def test_variant_stats_computes_win_loss_economics():
    # Two $100 bets at 0.50: one wins (+$100), one loses (-$100).
    portfolio = _portfolio_with([(100.0, True), (100.0, False)])
    stats = variant_stats("test@$500", {"profile": "aggressive", "whale_usd_threshold": 500.0}, portfolio)

    assert stats["closed_count"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["avg_win"] == 100.0
    assert stats["avg_loss"] == -100.0
    assert stats["profit_factor"] == 1.0
    assert stats["realized_pnl"] == 0.0


def test_variant_stats_profit_factor_none_when_nothing_lost():
    portfolio = _portfolio_with([(100.0, True)])
    stats = variant_stats("v", {}, portfolio)
    assert stats["profit_factor"] is None  # undefined, not infinity
    assert stats["avg_loss"] is None


def test_variant_stats_groups_finished_bets_by_stance():
    p = PaperPortfolio(1000.0)
    p.open_or_add(market_id="m1", token_id="t1", outcome="Yes", title="a", price=0.5, usd_amount=50, timestamp=1, stance="copy")
    p.resolve_position("t1", won=True, timestamp=2)
    p.open_or_add(market_id="m2", token_id="t2", outcome="Yes", title="b", price=0.5, usd_amount=50, timestamp=1, stance="fade")
    p.resolve_position("t2", won=False, timestamp=2)

    stats = variant_stats("v", {}, p)
    assert stats["by_stance"]["copy"]["win_rate"] == 1.0
    assert stats["by_stance"]["fade"]["win_rate"] == 0.0


def test_group_summary_aggregates_by_axis():
    rows = [
        {"profile": "aggressive", "return_pct": 10.0, "equity": 1100.0, "closed_count": 2, "wins": 2, "open_count": 1, "realized_pnl": 100.0},
        {"profile": "aggressive", "return_pct": -10.0, "equity": 900.0, "closed_count": 2, "wins": 0, "open_count": 0, "realized_pnl": -100.0},
        {"profile": "strict", "return_pct": 5.0, "equity": 1050.0, "closed_count": 1, "wins": 1, "open_count": 3, "realized_pnl": 50.0},
    ]
    groups = {g["profile"]: g for g in group_summary(rows, "profile")}

    assert groups["aggressive"]["variants"] == 2
    assert groups["aggressive"]["mean_return_pct"] == 0.0
    assert groups["aggressive"]["win_rate"] == 0.5   # 2 wins of 4 finished
    assert groups["strict"]["mean_return_pct"] == 5.0
    # sorted best-first
    assert group_summary(rows, "profile")[0]["profile"] == "strict"


def test_correlation_finds_threshold_return_relationship():
    rows = [
        {"whale_threshold": 500.0, "min_track_record": 3, "return_pct": 1.0, "closed_count": 1},
        {"whale_threshold": 1000.0, "min_track_record": 5, "return_pct": 2.0, "closed_count": 2},
        {"whale_threshold": 2500.0, "min_track_record": 10, "return_pct": 5.0, "closed_count": 3},
    ]
    result = correlations(rows)
    assert result["threshold_vs_return"] > 0.9  # higher threshold tracked higher return here


def test_load_run_and_report_reads_archived_runs(tmp_path):
    state = {
        "variants": {
            "a@$500": {"settings": {"profile": "aggressive", "whale_usd_threshold": 500.0},
                        "portfolio": _portfolio_with([(100.0, True)]).to_dict()},
        },
        "scorecard": {"0xabc": {"resolved_count": 1, "weighted_wins": 100.0, "weighted_total": 100.0}},
    }
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "experiment_state_20260101-000000.json").write_text(json.dumps(state))
    current = tmp_path / "experiment_state.json"
    current.write_text(json.dumps(state))

    config = dict(BASE_CONFIG)
    config["experiment"] = dict(BASE_CONFIG["experiment"])
    config["experiment"]["state_path"] = str(current)
    config["experiment"]["archive_dir"] = str(archive)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    assert len(build_report(str(config_path), include_archived=False)["runs"]) == 1
    assert len(build_report(str(config_path), include_archived=True)["runs"]) == 2

    run = load_run(current)
    assert run["scored_wallets"] == 1
    assert run["variants"][0]["name"] == "a@$500"


def test_old_state_files_without_stance_still_load(tmp_path):
    # ClosedTrade gained `stance` after some state files were already written.
    portfolio = _portfolio_with([(100.0, True)])
    raw = portfolio.to_dict()
    for trade in raw["closed_trades"]:
        del trade["stance"]
    restored = PaperPortfolio.from_dict(raw)
    assert restored.closed_trades[0].stance == "unknown"
