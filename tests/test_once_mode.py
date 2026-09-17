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

from src import experiment, spot_experiment


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
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    # Must return rather than loop; a hang here would fail by timeout.
    assert experiment.run_experiment("config.yaml", once=True) == 1


def test_spot_once_exits_nonzero_when_prices_are_unavailable(monkeypatch):
    _kill_network(monkeypatch)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    assert spot_experiment.run_spot_experiment("config.yaml", "forex", once=True) == 1


def test_runners_expose_a_once_flag():
    # The scheduled workflow depends on this flag existing on both entrypoints.
    import argparse, inspect
    for mod in (experiment, spot_experiment):
        src = inspect.getsource(mod.main)
        assert '"--once"' in src, f"{mod.__name__} lost its --once flag"
        assert "SystemExit" in src, f"{mod.__name__} does not propagate its exit code"
