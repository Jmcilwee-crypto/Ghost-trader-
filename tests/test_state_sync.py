"""The dashboard polls every few seconds but the bots now run on GitHub every
15 minutes, so the page has to pull their commits down. The failure that
matters is a silent one: sync stops working, the page keeps saying "updated
<now>", and stale numbers look live. These pin the states apart.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import state_sync
from src.state_sync import StateSync


@pytest.fixture
def sync():
    return StateSync(interval_seconds=60)


def _fake_git(responses):
    """responses: dict mapping the first git arg -> (code, output)."""
    calls = []

    def fake(*args, timeout=60):
        calls.append(args)
        for key, value in responses.items():
            if args[0] == key:
                return value
        return (0, "")

    fake.calls = calls
    return fake


def test_interval_has_a_floor_so_it_cannot_hammer_github(sync):
    assert StateSync(interval_seconds=1).interval == 30.0
    assert StateSync(interval_seconds=600).interval == 600.0


def test_up_to_date_reports_ok_without_merging(monkeypatch, sync):
    fake = _fake_git({"fetch": (0, ""), "rev-list": (0, "0")})
    monkeypatch.setattr(state_sync, "_git", fake)

    status = sync.sync_once()
    assert status["state"] == "ok"
    assert status["commits_pulled"] == 0
    assert status["last_success_at"] is not None
    # Nothing to fast-forward, so merge must not run at all.
    assert not any(c[0] == "merge" for c in fake.calls)


def test_new_commits_are_fast_forwarded_and_counted(monkeypatch, sync):
    fake = _fake_git({"fetch": (0, ""), "rev-list": (0, "3"), "merge": (0, "Fast-forward")})
    monkeypatch.setattr(state_sync, "_git", fake)

    status = sync.sync_once()
    assert status["state"] == "ok"
    assert status["commits_pulled"] == 3
    assert status["total_commits_pulled"] == 3
    assert "3 updates" in status["message"]

    # A second round adds to the running total rather than replacing it.
    sync.sync_once()
    assert sync.status()["total_commits_pulled"] == 6


def test_one_commit_is_not_pluralised(monkeypatch, sync):
    monkeypatch.setattr(state_sync, "_git",
                        _fake_git({"fetch": (0, ""), "rev-list": (0, "1"), "merge": (0, "")}))
    assert "1 update from" in sync.sync_once()["message"]


def test_merge_is_always_fast_forward_only(monkeypatch, sync):
    """A plain `git pull` could invent a merge commit in the user's repo."""
    fake = _fake_git({"fetch": (0, ""), "rev-list": (0, "2"), "merge": (0, "")})
    monkeypatch.setattr(state_sync, "_git", fake)
    sync.sync_once()

    merge = next(c for c in fake.calls if c[0] == "merge")
    assert "--ff-only" in merge


def test_divergence_is_surfaced_and_nothing_is_overwritten(monkeypatch, sync):
    fake = _fake_git({"fetch": (0, ""), "rev-list": (0, "2"),
                      "merge": (1, "fatal: Not possible to fast-forward, aborting.")})
    monkeypatch.setattr(state_sync, "_git", fake)

    status = sync.sync_once()
    assert status["state"] == "diverged"
    assert "Nothing was overwritten" in status["message"]
    # Never a reset/checkout that would throw away local work.
    assert not any(c[0] in ("reset", "checkout") for c in fake.calls)


def test_being_offline_is_reported_calmly_not_as_an_error(monkeypatch, sync):
    monkeypatch.setattr(state_sync, "_git",
                        _fake_git({"fetch": (128, "fatal: unable to access: Could not resolve host: github.com")}))
    status = sync.sync_once()
    assert status["state"] == "offline"
    assert "No internet" in status["message"]


def test_a_real_git_failure_is_reported_as_an_error(monkeypatch, sync):
    monkeypatch.setattr(state_sync, "_git",
                        _fake_git({"fetch": (128, "fatal: 'origin' does not appear to be a git repository")}))
    assert sync.sync_once()["state"] == "error"


def test_status_messages_never_leak_a_traceback(monkeypatch, sync):
    """The message is rendered to a non-technical user in the page header."""
    for responses in (
        {"fetch": (128, "Could not resolve host")},
        {"fetch": (0, ""), "rev-list": (0, "1"), "merge": (1, "not possible to fast-forward")},
        {"fetch": (0, ""), "rev-list": (0, "0")},
    ):
        monkeypatch.setattr(state_sync, "_git", _fake_git(responses))
        msg = StateSync().sync_once()["message"]
        assert "Traceback" not in msg and len(msg) < 200


def test_start_disables_itself_outside_a_repo(monkeypatch):
    monkeypatch.setattr(state_sync, "is_git_repo", lambda: False)
    s = StateSync()
    s.start()
    assert s.status()["enabled"] is False
    assert s.status()["state"] == "disabled"


def test_start_disables_itself_with_no_remote(monkeypatch):
    monkeypatch.setattr(state_sync, "is_git_repo", lambda: True)
    monkeypatch.setattr(state_sync, "has_remote", lambda: False)
    s = StateSync()
    s.start()
    assert s.status()["enabled"] is False


def test_status_returns_a_copy_so_callers_cannot_mutate_it(sync):
    sync.status()["state"] = "tampered"
    assert sync.status()["state"] != "tampered"


def test_dashboard_payload_carries_sync_state():
    """The page needs this key to tell stale data from live data."""
    import inspect
    from src import dashboard
    src = inspect.getsource(dashboard._make_handler)
    assert '"sync"' in src, "dashboard stopped reporting sync status to the page"
