"""Pulls bot state down from GitHub in the background.

Since the scheduled workflow took over, the bots no longer run on this
machine -- they run on GitHub's servers and commit their state back to the
repo. The dashboard reads local files, so without this it would sit showing
whatever was on disk when the laptop last ran a cycle, refreshing every five
seconds and never changing.

Deliberately fast-forward only. A `git pull` that can merge would let a
failed sync invent a merge commit in the user's repo; ff-only either applies
GitHub's commits cleanly or refuses and says why, which is the honest
outcome when local and remote have genuinely diverged.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Run a git command in the repo. Returns (returncode, combined output)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"git {' '.join(args)} timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "git is not installed"


def is_git_repo() -> bool:
    code, _ = _git("rev-parse", "--git-dir", timeout=10)
    return code == 0


def has_remote() -> bool:
    code, out = _git("remote", timeout=10)
    return code == 0 and bool(out)


class StateSync:
    """Background fast-forward puller with observable status.

    The status dict is what the dashboard renders, so every field is safe to
    show a non-technical user: a plain message, never a raw traceback.
    """

    def __init__(self, *, interval_seconds: float = 120.0, branch: str = "main"):
        self.interval = max(30.0, float(interval_seconds))
        self.branch = branch
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._status: dict = {
            "enabled": True,
            "state": "starting",
            "message": "Waiting for the first sync...",
            "last_success_at": None,
            "last_attempt_at": None,
            "commits_pulled": 0,
            "total_commits_pulled": 0,
        }

    # -- status ---------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)

    # -- the sync itself -------------------------------------------------
    def sync_once(self) -> dict:
        """One fetch + fast-forward. Returns the resulting status."""
        self._set(last_attempt_at=time.time())

        code, out = _git("fetch", "--quiet", "origin", self.branch)
        if code != 0:
            # Offline is the common case here and is not an error worth
            # alarming anyone about -- it resolves itself on the next tick.
            offline = any(s in out.lower() for s in
                          ("could not resolve", "network", "timed out", "connection"))
            self._set(
                state="offline" if offline else "error",
                message=("No internet -- showing the last data that reached this machine."
                         if offline else f"Could not reach GitHub: {out.splitlines()[-1][:120]}"
                         if out else "Could not reach GitHub."),
            )
            return self.status()

        code, before = _git("rev-parse", "HEAD")
        code2, behind = _git("rev-list", "--count", f"HEAD..origin/{self.branch}")
        pending = int(behind) if code2 == 0 and behind.isdigit() else 0

        if pending == 0:
            self._set(state="ok", message="Up to date with GitHub.",
                      commits_pulled=0, last_success_at=time.time())
            return self.status()

        code, out = _git("merge", "--ff-only", f"origin/{self.branch}")
        if code != 0:
            # Diverged: local has commits GitHub doesn't. Never resolve this
            # silently -- say so and leave the repo untouched.
            self._set(
                state="diverged",
                message=("This machine has local changes GitHub doesn't. "
                         "Nothing was overwritten -- ask Claude to reconcile them."),
            )
            return self.status()

        with self._lock:
            total = self._status["total_commits_pulled"] + pending
        self._set(state="ok", commits_pulled=pending, total_commits_pulled=total,
                  last_success_at=time.time(),
                  message=f"Pulled {pending} update{'s' if pending != 1 else ''} from GitHub.")
        return self.status()

    # -- threading -------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # never let the thread die silently
                self._set(state="error", message=f"Sync stopped unexpectedly: {exc}")
            self._stop.wait(self.interval)

    def start(self) -> None:
        if not is_git_repo():
            self._set(enabled=False, state="disabled",
                      message="Not a git repository -- nothing to sync from.")
            return
        if not has_remote():
            self._set(enabled=False, state="disabled",
                      message="No GitHub remote set -- nothing to sync from.")
            return
        self._thread = threading.Thread(target=self._loop, name="state-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
