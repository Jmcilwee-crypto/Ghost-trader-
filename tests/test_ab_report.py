"""The A/B report exists to stop the first version's mistake recurring silently.

That experiment printed a clean-looking "flat loses 4 of 5" while the arms had
started 12.5 days apart, so the number measured a head start. A report that
renders such a pair as a result is worse than no report, so the confound check
is the part under test here.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.stats import ab_report

DAY = 86400.0
BASE = 1_800_000_000.0


def _arm(pair, arm, *, cash, first_bet, n=10, flat=None):
    return {
        "settings": {"ab_pair": pair, "ab_arm": arm, "flat_stake_usd": flat},
        "portfolio": {
            "starting_cash": 1000.0, "cash": cash, "positions": {},
            "closed_trades": [
                {"pnl": (cash - 1000.0) / n, "avg_price": 0.5, "shares": 40.0,
                 "opened_at": first_bet + i * 60}
                for i in range(n)
            ],
        },
    }


def _write(tmp_path, variants):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"variants": variants}))
    return str(p)


def test_a_pair_started_together_is_valid_evidence(tmp_path):
    state = _write(tmp_path, {
        "ab-x-scaled": _arm("x", "scaled", cash=1050.0, first_bet=BASE),
        "ab-x-flat": _arm("x", "flat", cash=1090.0, first_bet=BASE + 600, flat=20.0),
    })
    row = ab_report(state)[0]
    assert row["valid"] is True
    assert row["diff_pp"] == pytest.approx(4.0)
    assert "flat ahead" in row["note"]


def test_a_staggered_pair_is_refused_not_reported(tmp_path):
    """The exact shape of the original mistake."""
    state = _write(tmp_path, {
        "ab-x-scaled": _arm("x", "scaled", cash=1270.0, first_bet=BASE),
        "ab-x-flat": _arm("x", "flat", cash=1080.0, first_bet=BASE + 12.5 * DAY, flat=20.0),
    })
    row = ab_report(state)[0]
    assert row["valid"] is False
    assert "CONFOUNDED" in row["note"]
    assert "12.5 days apart" in row["note"]
    # Crucially, no comparison number is offered at all.
    assert "diff_pp" not in row


def test_a_small_start_gap_is_tolerated(tmp_path):
    """Arms rarely place their first bet in the same minute; hours are fine."""
    state = _write(tmp_path, {
        "ab-x-scaled": _arm("x", "scaled", cash=1050.0, first_bet=BASE),
        "ab-x-flat": _arm("x", "flat", cash=1060.0, first_bet=BASE + 6 * 3600, flat=20.0),
    })
    assert ab_report(state)[0]["valid"] is True


def test_an_arm_with_no_settled_bets_is_not_yet_evidence(tmp_path):
    state = _write(tmp_path, {
        "ab-x-scaled": _arm("x", "scaled", cash=1050.0, first_bet=BASE),
        "ab-x-flat": _arm("x", "flat", cash=1000.0, first_bet=BASE, n=0, flat=20.0),
    })
    row = ab_report(state)[0]
    assert row["valid"] is False
    assert "not enough data" in row["note"]


def test_a_missing_arm_is_reported_as_incomplete(tmp_path):
    state = _write(tmp_path, {"ab-x-scaled": _arm("x", "scaled", cash=1050.0, first_bet=BASE)})
    row = ab_report(state)[0]
    assert row["valid"] is False
    assert "incomplete" in row["note"]


def test_non_cohort_bots_are_ignored(tmp_path):
    state = _write(tmp_path, {
        "aggressive@$500": {"settings": {"profile": "aggressive"},
                            "portfolio": {"starting_cash": 1000.0, "cash": 1200.0,
                                          "positions": {}, "closed_trades": []}},
        "ab-x-scaled": _arm("x", "scaled", cash=1050.0, first_bet=BASE),
        "ab-x-flat": _arm("x", "flat", cash=1060.0, first_bet=BASE, flat=20.0),
    })
    rows = ab_report(state)
    assert len(rows) == 1 and rows[0]["pair"] == "x"


def test_open_positions_count_toward_the_comparison(tmp_path):
    """Comparing realised P&L alone would flatter whichever arm holds losers."""
    variants = {
        "ab-x-scaled": _arm("x", "scaled", cash=1000.0, first_bet=BASE, n=1),
        "ab-x-flat": _arm("x", "flat", cash=900.0, first_bet=BASE, n=1, flat=20.0),
    }
    variants["ab-x-flat"]["portfolio"]["positions"] = {
        "tok": {"shares": 400.0, "avg_price": 0.5}      # $200 held, so equity is $1,100
    }
    row = ab_report(_write(tmp_path, variants))[0]
    assert row["flat"]["return_pct"] == pytest.approx(10.0)
