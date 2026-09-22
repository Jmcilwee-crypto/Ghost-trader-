"""The tranche sizing inside the trade loop once reused the name `target`,
which is also the bankroll goal the loop breaks on. A catalyst bot's first
tranche was ~$75, so after that buy the stop test read
`equity >= 75` -- true for every bot -- and the whole asset class shut down
mid-experiment reporting it had "reached the $75 target".

This pins the two apart: the module must not rebind the loop's stop target,
and a run that places a tranched buy must keep going.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import spot_experiment


def _run_spot_experiment_fn() -> ast.FunctionDef:
    tree = ast.parse(Path(spot_experiment.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_spot_experiment":
            return node
    raise AssertionError("run_spot_experiment not found")


def test_target_is_assigned_exactly_once_from_the_bankroll_config():
    """Any second assignment to `target` inside the loop is the bug returning."""
    fn = _run_spot_experiment_fn()
    assignments = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id == "target"
    ]
    assert len(assignments) == 1, (
        f"`target` is assigned {len(assignments)} times in run_spot_experiment; "
        "it is the bankroll stop threshold and must not be reused for tranche sizing"
    )
    src = ast.unparse(assignments[0].value)
    assert "bankroll" in src and "target_usd" in src, (
        f"`target` should come from the bankroll config, got: {src}"
    )


def test_tranche_sizing_uses_its_own_name():
    fn = _run_spot_experiment_fn()
    names = {
        t.id for node in ast.walk(fn) if isinstance(node, ast.Assign)
        for t in node.targets if isinstance(t, ast.Name)
    }
    assert "tranche_target" in names, "tranche sizing lost its distinct variable name"


def test_a_tranche_sized_buy_does_not_satisfy_the_bankroll_target():
    """The regression in plain arithmetic: one tranche is a few tens of dollars,
    the bankroll goal is thousands. If the two ever share a variable, every bot
    trivially clears the goal on its first catalyst buy."""
    starting, goal = 1000.0, 10_000.0
    max_pct_per_market, first_tranche = 0.30, 0.25
    tranche_target = starting * max_pct_per_market * first_tranche
    assert tranche_target < 100  # ~$75, the size that triggered the shutdown
    assert starting < goal       # a $1,000 bot has NOT reached a $10,000 goal
    assert starting >= tranche_target  # ...but trivially clears a $75 one
