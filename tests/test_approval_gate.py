"""Human-review gate tests for LLM-generated strategy code (Cleanup 16-C).

Seams under test:
- ``runner.approval_gate.approve_strategy`` / ``is_strategy_approved`` /
  ``require_approval``: the on-disk SHA256-bound approval ledger.
"""

from __future__ import annotations

import pytest

from runner.approval_gate import (
    StrategyNotApprovedError,
    approve_strategy,
    approval_path_for,
    is_strategy_approved,
    require_approval,
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
    approve_strategy(strategy, strategies_root=tmp_path)
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
    approve_strategy(first, strategies_root=tmp_path)

    assert is_strategy_approved(first, strategies_root=tmp_path)
    assert not is_strategy_approved(second, strategies_root=tmp_path)


def test_missing_strategy_file_is_not_approved(tmp_path):
    assert not is_strategy_approved(tmp_path / "ghost.py", strategies_root=tmp_path)


def test_approval_path_is_deterministic_per_file(tmp_path):
    assert (
        approval_path_for(tmp_path / "demo.py", strategies_root=tmp_path).name
        == "demo.py.approved.json"
    )
