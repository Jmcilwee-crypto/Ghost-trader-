import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scorecard import TraderScorecard


def test_no_track_record_before_resolution():
    sc = TraderScorecard()
    sc.observe_trade(wallet="w1", market_id="m1", token_id="yes", usd_size=100, price=0.5)
    assert sc.accuracy("w1") is None
    assert sc.resolved_count("w1") == 0
    assert not sc.has_track_record("w1", min_count=1)


def test_resolution_updates_accuracy_weighted_by_size():
    sc = TraderScorecard()
    sc.observe_trade(wallet="w1", market_id="m1", token_id="yes", usd_size=100, price=0.5)  # will win
    sc.observe_trade(wallet="w1", market_id="m2", token_id="no", usd_size=300, price=0.5)   # will lose
    sc.resolve_market("m1", winning_token_id="yes")
    sc.resolve_market("m2", winning_token_id="yes")  # w1 bet "no" and lost
    # weighted: 100 win / 400 total = 0.25
    assert sc.resolved_count("w1") == 2
    assert round(sc.accuracy("w1"), 4) == 0.25


def test_drop_market_discards_pending_without_scoring():
    sc = TraderScorecard()
    sc.observe_trade(wallet="w1", market_id="m1", token_id="yes", usd_size=100, price=0.5)
    sc.drop_market("m1")
    sc.resolve_market("m1", winning_token_id="yes")  # no-op, nothing pending
    assert sc.resolved_count("w1") == 0


def test_serialization_roundtrip():
    sc = TraderScorecard()
    sc.observe_trade(wallet="w1", market_id="m1", token_id="yes", usd_size=100, price=0.5)
    sc.resolve_market("m1", winning_token_id="yes")
    data = sc.to_dict()

    sc2 = TraderScorecard()
    sc2.load_dict(data)
    assert sc2.accuracy("w1") == sc.accuracy("w1")
    assert sc2.resolved_count("w1") == sc.resolved_count("w1")


def test_pending_bets_survive_a_restart():
    # Prediction markets stay open for days. If the in-flight ledger is lost
    # on restart, nobody is ever scored when those markets settle -- which
    # silently disables copy/fade entirely.
    sc = TraderScorecard()
    sc.observe_trade(wallet="whale", market_id="m1", token_id="yes", usd_size=500, price=0.6)
    assert sc.pending_count() == 1

    restored = TraderScorecard()
    restored.load_dict(sc.to_dict())
    assert restored.pending_count() == 1

    # Resolving after the restart must still credit the wallet.
    restored.resolve_market("m1", winning_token_id="yes")
    assert restored.resolved_count("whale") == 1
    assert restored.accuracy("whale") == 1.0


def test_old_state_files_without_the_wallets_key_still_load():
    legacy = {"w1": {"resolved_count": 3, "weighted_wins": 200.0, "weighted_total": 400.0}}
    sc = TraderScorecard()
    sc.load_dict(legacy)
    assert sc.resolved_count("w1") == 3
    assert sc.accuracy("w1") == 0.5


def test_load_dict_tolerates_empty_state():
    sc = TraderScorecard()
    sc.load_dict({})
    assert sc.pending_count() == 0
