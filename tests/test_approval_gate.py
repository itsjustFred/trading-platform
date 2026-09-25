"""Human-review gate tests for LLM-generated strategy code (Cleanup 16-C).

Seams under test:
- ``runner.approval_gate.approve_strategy`` / ``is_strategy_approved`` /
  ``require_approval``: the on-disk SHA256-bound approval ledger.
"""

from __future__ import annotations

import pytest

import runner.auto_agent as auto_agent
from runner.approval_gate import (
    StrategyNotApprovedError,
    approval_path_for,
    approve_strategy,
    generated_marker_path_for,
    is_generated_strategy,
    is_strategy_approved,
    mark_generated_strategy,
    require_approval,
    require_generated_strategy_approval,
)


def test_unreviewed_strategy_is_not_approved(tmp_path):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    assert not is_strategy_approved(strategy, strategies_root=tmp_path)


def test_approval_is_bound_to_file_hash(tmp_path):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)

    assert is_strategy_approved(strategy, strategies_root=tmp_path)

    strategy.write_text("import os\nos.system('boom')\n")
    assert not is_strategy_approved(strategy, strategies_root=tmp_path)


def test_generated_source_remains_generated_after_marker_deletion(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text(
        "STRATEGY_META = {'provenance': {'generated': True}}\n",
        encoding="utf-8",
    )
    mark_generated_strategy(strategy, strategies_root=tmp_path)
    generated_marker_path_for(strategy).unlink()

    assert is_generated_strategy(strategy)
    assert not is_strategy_approved(strategy, strategies_root=tmp_path)

    with pytest.raises(StrategyNotApprovedError, match="marker or provenance"):
        require_generated_strategy_approval(strategy, strategies_root=tmp_path)


def test_generated_approval_does_not_become_manual_after_sidecar_deletion(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text(
        "STRATEGY_META = {'provenance': {'generated': True}}\n",
        encoding="utf-8",
    )
    mark_generated_strategy(strategy, strategies_root=tmp_path)
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)
    generated_marker_path_for(strategy).unlink()

    with pytest.raises(StrategyNotApprovedError, match="marker or provenance"):
        require_generated_strategy_approval(strategy, strategies_root=tmp_path)


def test_malformed_generated_marker_fails_closed(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text("x = 1\n", encoding="utf-8")
    generated_marker_path_for(strategy).write_text("not-json\n", encoding="utf-8")

    with pytest.raises(StrategyNotApprovedError, match="unreadable"):
        require_generated_strategy_approval(strategy, strategies_root=tmp_path)


def test_generated_marker_with_wrong_provenance_fails_closed(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text("x = 1\n", encoding="utf-8")
    mark_generated_strategy(strategy, strategies_root=tmp_path)
    marker = generated_marker_path_for(strategy)
    marker.write_text(
        marker.read_text(encoding="utf-8").replace('"auto_agent"', '"manual"'),
        encoding="utf-8",
    )

    with pytest.raises(StrategyNotApprovedError, match="invalid provenance"):
        require_generated_strategy_approval(strategy, strategies_root=tmp_path)


def test_force_does_not_bypass_generated_marker_requirement(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text(
        "STRATEGY_META = {'provenance': {'generated': True}}\n",
        encoding="utf-8",
    )
    mark_generated_strategy(strategy, strategies_root=tmp_path)
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)
    generated_marker_path_for(strategy).unlink()

    with pytest.raises(StrategyNotApprovedError, match="marker or provenance"):
        require_generated_strategy_approval(
            strategy, strategies_root=tmp_path, force=True
        )


def test_generated_marker_without_approval_still_fails_closed(tmp_path):
    strategy = tmp_path / "generated.py"
    strategy.write_text("x = 1\n", encoding="utf-8")
    mark_generated_strategy(strategy, strategies_root=tmp_path)

    with pytest.raises(StrategyNotApprovedError, match="human review"):
        require_generated_strategy_approval(strategy, strategies_root=tmp_path)


def test_approval_ledger_records_hash_reviewer_and_timestamp(tmp_path):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    record = approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)

    assert record["sha256"]
    assert record["approved_by"] == "alice"
    assert record["approved_at"]
    assert record["file"] == "demo.py"


def test_require_approval_passes_for_approved_hash_and_raises_after_change(tmp_path):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)
    require_approval(strategy, strategies_root=tmp_path)

    strategy.write_text("x = 2\n")
    with pytest.raises(StrategyNotApprovedError) as excinfo:
        require_approval(strategy, strategies_root=tmp_path)
    assert "human review" in str(excinfo.value)


def test_approval_is_scoped_per_strategy_file(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("x = 1\n")
    second.write_text("y = 2\n")
    approve_strategy(first, approved_by="alice", strategies_root=tmp_path)

    assert is_strategy_approved(first, strategies_root=tmp_path)
    assert not is_strategy_approved(second, strategies_root=tmp_path)


def test_missing_strategy_file_is_not_approved(tmp_path):
    assert not is_strategy_approved(tmp_path / "ghost.py", strategies_root=tmp_path)


def test_approval_path_is_deterministic_per_file(tmp_path):
    assert approval_path_for(tmp_path / "demo.py").name == "demo.py.approved.json"


def test_discarded_strategy_leaves_no_orphaned_ledger(tmp_path):
    """Removing a generated file must remove its approval ledger with it."""
    from runner.approval_gate import approval_path_for
    from runner.auto_agent import _discard_failed_attempt

    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)
    assert approval_path_for(strategy).exists()

    strategy.unlink()
    # Last failed attempt: the whole slug directory (ledger included) is gone.
    _discard_failed_attempt(
        attempt=3,
        max_attempts=3,
        last_error="boom",
        artifacts=auto_agent.StrategyArtifacts(
            resolved_name="demo",
            engine="vectorbt",
            strategy_dir=tmp_path,
            strategy_path=strategy,
            documentation_path=tmp_path / "README.md",
            output_path=tmp_path / "out.json",
        ),
    )
    assert not approval_path_for(strategy).exists()


def test_ledger_for_a_different_file_is_not_accepted(tmp_path, monkeypatch):
    """A ledger copied beside another file never legitimizes that file."""
    from runner.approval_gate import approval_path_for, is_strategy_approved

    first = tmp_path / "first.py"
    first.write_text("x = 1\n")
    approve_strategy(first, approved_by="alice", strategies_root=tmp_path)

    second = tmp_path / "second.py"
    second.write_text("x = 1\n")  # identical content, identical hash
    # Simulate a copied/left-behind ledger: same bytes, wrong provenance.
    (approval_path_for(second)).write_text(approval_path_for(first).read_text())

    assert is_strategy_approved(first, strategies_root=tmp_path)
    assert not is_strategy_approved(second, strategies_root=tmp_path)


@pytest.mark.parametrize("reviewer", [None, "", "   ", "unspecified"])
def test_approval_requires_non_empty_reviewer(tmp_path, reviewer):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")

    with pytest.raises(ValueError, match="approved_by"):
        approve_strategy(strategy, approved_by=reviewer, strategies_root=tmp_path)

    assert not approval_path_for(strategy).exists()


def test_approval_ledger_write_is_atomic(tmp_path, monkeypatch):
    strategy = tmp_path / "demo.py"
    strategy.write_text("x = 1\n")
    approve_strategy(strategy, approved_by="alice", strategies_root=tmp_path)
    ledger = approval_path_for(strategy)
    original = ledger.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("runner.approval_gate.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        approve_strategy(strategy, approved_by="bob", strategies_root=tmp_path)

    assert ledger.read_bytes() == original
    assert not list(tmp_path.glob(".demo.py.approved.json.*.tmp"))
