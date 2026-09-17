import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.dashboard_data import build_dashboard_data
from src.portfolio import PaperPortfolio

BASE_CONFIG = yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text())


def _write_experiment_fixture(tmp_path, variants):
    """variants: {name: (settings, PaperPortfolio)}"""
    state = {
        "variants": {
            name: {"settings": settings, "portfolio": portfolio.to_dict()}
            for name, (settings, portfolio) in variants.items()
        },
        "scorecard": {"0xaaa": {"resolved_count": 3, "weighted_wins": 200.0, "weighted_total": 300.0}},
        "last_seen_ts": 123.0,
        "seen_hashes": [],
    }
    state_path = tmp_path / "experiment_state.json"
    state_path.write_text(json.dumps(state))

    config = dict(BASE_CONFIG)
    config["experiment"] = dict(BASE_CONFIG["experiment"])
    config["experiment"]["state_path"] = str(state_path)
    config["experiment"]["flagged_log_path"] = str(tmp_path / "flagged.jsonl")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return str(config_path)


def _portfolio(cash, positions=(), closed=()):
    p = PaperPortfolio(1000.0)
    p.cash = cash
    for token_id, market_id, usd in positions:
        p.cash += usd  # open_or_add deducts it again
        p.open_or_add(market_id=market_id, token_id=token_id, outcome="Yes", title=f"Market {market_id}",
                      price=0.5, usd_amount=usd, timestamp=1, stance="copy")
    for token_id, won in closed:
        p.cash += 10.0
        p.open_or_add(market_id="m", token_id=token_id, outcome="Yes", title="Resolved market",
                      price=0.5, usd_amount=10.0, timestamp=1, stance="copy")
        p.resolve_position(token_id, won=won, timestamp=2)
    return p


def test_reports_no_data_when_state_file_missing(tmp_path):
    config = dict(BASE_CONFIG)
    config["experiment"] = dict(BASE_CONFIG["experiment"])
    config["experiment"]["state_path"] = str(tmp_path / "nope.json")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    result = build_dashboard_data(str(config_path))
    assert result["has_data"] is False
    assert "src.experiment" in result["message"]


def test_leaderboard_ranks_variants_by_equity(tmp_path):
    config_path = _write_experiment_fixture(tmp_path, {
        "loser@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0}, _portfolio(800.0)),
        "winner@$1000": ({"profile": "balanced", "whale_usd_threshold": 1000.0}, _portfolio(1500.0)),
        "middle@$2500": ({"profile": "strict", "whale_usd_threshold": 2500.0}, _portfolio(1100.0)),
    })
    data = build_dashboard_data(config_path)

    assert data["has_data"] is True
    names = [row["name"] for row in data["leaderboard"]]
    assert names == ["winner@$1000", "middle@$2500", "loser@$500"]
    assert data["summary"]["leader_name"] == "winner@$1000"
    assert data["summary"]["equity"] == 1500.0
    assert data["summary"]["change_pct"] == 50.0


def test_every_variant_carries_its_own_positions_and_settings(tmp_path):
    config_path = _write_experiment_fixture(tmp_path, {
        "quiet@$5000": ({"profile": "strict", "whale_usd_threshold": 5000.0, "min_track_record": 10}, _portfolio(1000.0)),
        "busy@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0, "min_track_record": 3},
                       _portfolio(1200.0, positions=[("tokA", "mktA", 40.0)])),
    })
    data = build_dashboard_data(config_path)
    by_name = {r["name"]: r for r in data["leaderboard"]}

    assert data["summary"]["leader_name"] == "busy@$500"
    assert len(by_name["busy@$500"]["positions"]) == 1
    assert by_name["busy@$500"]["positions"][0]["title"] == "Market mktA"
    assert by_name["quiet@$5000"]["positions"] == []
    # Each row exposes the knobs that make that bot different.
    assert by_name["quiet@$5000"]["settings"]["min_track_record"] == 10
    assert by_name["busy@$500"]["settings"]["whale_usd_threshold"] == 500.0


def test_value_bot_settings_survive_to_the_dashboard(tmp_path):
    # Value bots are tuned by different knobs than whale bots; cherry-picking
    # a fixed key list silently dropped these.
    config_path = _write_experiment_fixture(tmp_path, {
        "value-edge10": ({"profile": "value", "whale_usd_threshold": 500.0,
                           "min_edge": 0.10, "kelly_fraction": 0.25}, _portfolio(1000.0)),
    })
    data = build_dashboard_data(config_path)
    settings = data["leaderboard"][0]["settings"]

    assert settings["min_edge"] == 0.10
    assert settings["kelly_fraction"] == 0.25
    assert settings["profile"] == "value"


def test_positions_report_their_best_case_upside(tmp_path):
    # Bought at 0.50, so a win doubles the stake: +100%.
    config_path = _write_experiment_fixture(tmp_path, {
        "a@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0},
                    _portfolio(1000.0, positions=[("tokA", "mktA", 40.0)])),
    })
    data = build_dashboard_data(config_path)
    assert data["leaderboard"][0]["positions"][0]["max_gain_pct"] == 100.0


def test_summary_totals_exposure_across_every_variant(tmp_path):
    config_path = _write_experiment_fixture(tmp_path, {
        "a@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0},
                    _portfolio(1000.0, positions=[("tokA", "mktA", 40.0)])),
        "b@$1000": ({"profile": "balanced", "whale_usd_threshold": 1000.0},
                     _portfolio(1000.0, positions=[("tokB", "mktB", 25.0)])),
    })
    data = build_dashboard_data(config_path)
    assert data["summary"]["total_open_all_variants"] == 2
    assert data["summary"]["total_exposure_all_variants"] == 65.0


def test_ties_break_toward_variants_that_have_actually_traded(tmp_path):
    # Every variant starts at the same equity, so without a tiebreak the same
    # arbitrary variant would always appear to be "winning".
    config_path = _write_experiment_fixture(tmp_path, {
        "idle@$10000": ({"profile": "strict", "whale_usd_threshold": 10000.0}, _portfolio(1000.0)),
        "active@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0},
                         _portfolio(1000.0, positions=[("tokA", "mktA", 25.0)])),
    })
    data = build_dashboard_data(config_path)
    assert data["leaderboard"][0]["name"] == "active@$500"


def test_summary_aggregates_across_all_variants(tmp_path):
    config_path = _write_experiment_fixture(tmp_path, {
        "a@$500": ({"profile": "aggressive", "whale_usd_threshold": 500.0},
                    _portfolio(1000.0, closed=[("t1", True), ("t2", False)])),
        "b@$1000": ({"profile": "balanced", "whale_usd_threshold": 1000.0},
                     _portfolio(1000.0, closed=[("t3", True)])),
        "c@$2500": ({"profile": "strict", "whale_usd_threshold": 2500.0}, _portfolio(1000.0)),
    })
    data = build_dashboard_data(config_path)
    s = data["summary"]

    assert s["variant_count"] == 3
    assert s["variants_trading"] == 2       # "c" has done nothing
    assert s["total_closed_all_variants"] == 3
    assert s["overall_win_rate"] == 2 / 3
    assert s["known_wallets"] == 1


def test_wallet_count_reads_the_wrapped_scorecard_format(tmp_path):
    # The scorecard gained a {wallets, pending} wrapper. Counting the raw dict
    # reported "2" no matter how many traders had actually been scored.
    from src.dashboard_data import _scored_wallets

    wrapped = {"wallets": {"0xa": {}, "0xb": {}, "0xc": {}}, "pending": {"m1": [{}, {}]}}
    assert _scored_wallets(wrapped) == 3
    assert _scored_wallets({"wallets": {}, "pending": {"m1": [{}]}}) == 0
    assert _scored_wallets({"0xa": {}, "0xb": {}}) == 2      # legacy flat format
    assert _scored_wallets({}) == 0
    assert _scored_wallets(None) == 0
