"""--once mode exists so a scheduler (GitHub Actions, cron) can be the loop.

The subtle failure: the data-fetch error path ends in `continue`, which jumps
straight back to the top of the while-loop and skips the once-check at the
bottom. A blocked data source therefore retried forever instead of exiting --
under CI that burns the whole job timeout on every scheduled run, for all five
experiments. These pin the exit instead.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests
import yaml

from src import experiment, spot_experiment

ROOT = Path(__file__).resolve().parents[1]


def _isolated_config(tmp_path, asset_class=None):
    """A copy of the real config with every data path moved into tmp_path.

    Pointing a test at the live config made it read -- and, once persisted
    bars gave the spot runner something to fall back on, overwrite -- the
    running bots' own state files in data/. It also made this test's premise
    false: "no network" stopped meaning "no prices", because the last real
    run had left bars on disk for it to reuse.
    """
    config = yaml.safe_load((ROOT / "config.yaml").read_text())

    def relocate(node):
        if isinstance(node, dict):
            return {k: relocate(v) for k, v in node.items()}
        if isinstance(node, list):
            return [relocate(v) for v in node]
        if isinstance(node, str) and node.startswith("data/"):
            return str(tmp_path / node)
        return node

    config = relocate(config)
    if asset_class:
        # These three are defaulted in code rather than listed in the file,
        # so relocating strings alone would leave them pointing at data/.
        config.setdefault("spot", {}).update({
            "state_path": str(tmp_path / f"spot_{asset_class}_state.json"),
            "history_path": str(tmp_path / f"spot_{asset_class}_history.jsonl"),
            "flagged_log_path": str(tmp_path / f"spot_{asset_class}_flagged.jsonl"),
        })
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


def _kill_network(monkeypatch):
    def dead(*args, **kwargs):
        raise requests.ConnectionError("simulated block")
    monkeypatch.setattr(requests, "get", dead)
    monkeypatch.setattr(requests.Session, "get", dead)


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch):
    """Running a full cycle calls load_env_file(), which populates the real API
    keys into os.environ for the rest of the session and quietly breaks any
    later test asserting "no key is set". Snapshot and restore around it."""
    before = dict(os.environ)
    yield
    for key in set(os.environ) - set(before):
        monkeypatch.delenv(key, raising=False)


def test_polymarket_once_exits_nonzero_when_the_feed_is_blocked(monkeypatch, tmp_path):
    _kill_network(monkeypatch)
    monkeypatch.chdir(ROOT)
    # Must return rather than loop; a hang here would fail by timeout.
    assert experiment.run_experiment(_isolated_config(tmp_path), once=True) == 1


def test_spot_once_exits_nonzero_when_prices_are_unavailable(monkeypatch, tmp_path):
    _kill_network(monkeypatch)
    monkeypatch.chdir(ROOT)
    cfg = _isolated_config(tmp_path, asset_class="forex")
    assert spot_experiment.run_spot_experiment(cfg, "forex", once=True) == 1


def test_a_blocked_cycle_does_not_touch_the_live_state_files(monkeypatch, tmp_path):
    """The regression that hid the bug above: tests writing into data/."""
    _kill_network(monkeypatch)
    monkeypatch.chdir(ROOT)
    live = ROOT / "data" / "spot_forex_state.json"
    before = live.read_bytes() if live.exists() else None
    spot_experiment.run_spot_experiment(_isolated_config(tmp_path, "forex"), "forex", once=True)
    after = live.read_bytes() if live.exists() else None
    assert after == before, "the test suite overwrote a running bot's state"


def test_runners_expose_a_once_flag():
    # The scheduled workflow depends on this flag existing on both entrypoints.
    import argparse, inspect
    for mod in (experiment, spot_experiment):
        src = inspect.getsource(mod.main)
        assert '"--once"' in src, f"{mod.__name__} lost its --once flag"
        assert "SystemExit" in src, f"{mod.__name__} does not propagate its exit code"
