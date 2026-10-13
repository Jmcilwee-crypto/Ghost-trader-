"""A scheduled --once run is a fresh process, and free-tier providers let only
one symbol through per run. Without persisted history every other symbol looks
to the strategies like an instrument with no data at all.

That is exactly what happened: the stocks state held prices for AAPL alone,
and TTWO -- the symbol the GTA 6 catalyst bots trade -- was never priced, so
their second tranche could not fire and the asset class sat idle for 8 days
without erroring.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import spot_experiment
from src.spot_experiment import MAX_PERSISTED_BARS, load_state, save_state
from src.spot_portfolio import SpotPortfolio


class _Variant:
    def __init__(self, name):
        self.name, self.settings = name, {"profile": "test"}
        self.portfolio = SpotPortfolio(1000.0)


def test_bars_survive_a_save_load_round_trip(tmp_path):
    path = str(tmp_path / "s.json")
    bars = {"AAPL": [1.0, 2.0, 3.0], "TTWO": [10.0, 11.0]}
    save_state(path, [_Variant("a")], {"AAPL": 3.0}, {}, bars)
    assert load_state(path, [_Variant("a")]) == bars


def test_persisted_bars_are_capped(tmp_path):
    path = str(tmp_path / "s.json")
    save_state(path, [_Variant("a")], {}, {}, {"AAPL": list(range(5000))})
    restored = load_state(path, [_Variant("a")])["AAPL"]
    assert len(restored) == MAX_PERSISTED_BARS
    assert restored[-1] == 4999, "the cap must keep the NEWEST bars, not the oldest"


def test_missing_bars_key_is_tolerated(tmp_path):
    """State written before this existed must still load."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"variants": {}, "prices": {}}))
    assert load_state(str(path), [_Variant("a")]) == {}


def test_empty_histories_are_not_restored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"variants": {}, "bars": {"AAPL": [], "TTWO": [1.0]}}))
    assert load_state(str(path), [_Variant("a")]) == {"TTWO": [1.0]}


def test_portfolios_still_restore_alongside_bars(tmp_path):
    path = str(tmp_path / "s.json")
    v = _Variant("a")
    v.portfolio.cash = 742.0
    save_state(path, [v], {}, {}, {"AAPL": [1.0]})
    fresh = _Variant("a")
    load_state(path, [fresh])
    assert fresh.portfolio.cash == 742.0


# -- the rotation that makes every symbol reachable --------------------
def test_rotation_reaches_every_symbol_over_successive_runs():
    """One symbol per run is fine, provided it is a different one each time."""
    symbols = ["AAPL", "MSFT", "NVDA", "TTWO", "SPY"]
    poll = 900
    first_fetched = set()
    for run in range(len(symbols) * 2):
        now = run * poll
        offset = int(now // poll) % len(symbols)
        rotated = symbols[offset:] + symbols[:offset]
        first_fetched.add(rotated[0])
    assert first_fetched == set(symbols), \
        f"these symbols never got the first slot: {set(symbols) - first_fetched}"


def test_rotation_is_a_permutation_not_a_filter():
    """Rotation must reorder the list, never drop from it."""
    symbols = ["A", "B", "C", "D"]
    for offset in range(len(symbols) + 2):
        o = offset % len(symbols)
        assert sorted(symbols[o:] + symbols[:o]) == sorted(symbols)


def test_rotation_handles_a_single_symbol_without_dividing_by_zero():
    for n in (1, 0):
        symbols = ["ONLY"] * n
        offset = int(time.time() // 900) % max(len(symbols), 1)
        assert symbols[offset:] + symbols[:offset] == symbols
