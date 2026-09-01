"""Script-hygiene tests (Cleanup 20-D).

Covers: mock-result discovery output, matrix queue runner lock acquisition,
and dedupe_executions dry-run discipline.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_script(name: str):
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"scripts_{name.replace('.py', '')}", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── overnight_strategy_discovery ─────────────────────────────────────────────


def test_discovery_does_not_write_mock_results(tmp_path: Path, monkeypatch) -> None:
    """The discovery script must not publish mock rows into the consumed directory."""
    monkeypatch.setattr(sys, "path", sys.path.copy())
    module = _load_script("overnight_strategy_discovery.py")
    output_dir = tmp_path / "discovered_strategies"
    monkeypatch.setattr(module, "DISCOVERED_DIR", output_dir)

    results = module.run_discovery_batch(n_trials=3)

    assert results, "the batch API still returns trial rows for local review"
    assert not output_dir.exists(), "mock results must not be written for downstream runs"
    for row in results:
        assert row.get("status") == "mock"


def test_discovery_requires_explicit_flag_to_write(tmp_path: Path, monkeypatch) -> None:
    """Writing requires --publish, and published rows carry status 'mock'."""
    module = _load_script("overnight_strategy_discovery.py")
    output_dir = tmp_path / "discovered_strategies"
    monkeypatch.setattr(module, "DISCOVERED_DIR", output_dir)

    module.run_discovery_batch(n_trials=2, publish=True)

    files = list(output_dir.glob("discovery_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload
    assert all(row.get("status") == "mock" for row in payload)


# ─── matrix_queue_runner lock ─────────────────────────────────────────────────


@pytest.fixture()
def queue_runner(tmp_path: Path, monkeypatch):
    module = _load_script("matrix_queue_runner.py")
    matrix_dir = tmp_path / "matrix"
    monkeypatch.setattr(module, "MATRIX_DIR", matrix_dir)
    monkeypatch.setattr(module, "STATE_PATH", matrix_dir / "queue_state.json")
    monkeypatch.setattr(module, "LOCK_PATH", matrix_dir / "queue.lock")
    return module


def test_acquire_lock_uses_flock_exclusivity(queue_runner) -> None:
    """A held flock blocks a second acquirer without any unlink race."""
    module = queue_runner

    first = module.acquire_lock()
    assert first is True
    assert module.LOCK_PATH.exists()

    second = module.acquire_lock()
    assert second is False, "second session must not steal the live lock"


def test_acquire_lock_never_deletes_a_live_lock(queue_runner) -> None:
    """A fresh lock file written by another session blocks acquisition untouched."""
    module = queue_runner
    module.MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text("2099-01-01T00:00:00+00:00", encoding="utf-8")
    fresh_age = 1.0  # far below the TTL, so the file looks freshly written

    with patch.object(module, "_lock_age_seconds", return_value=fresh_age):
        acquired = module.acquire_lock()

    assert acquired is False
    assert module.LOCK_PATH.exists(), "a live lock must never be deleted"
    assert module.LOCK_PATH.read_text(encoding="utf-8") == "2099-01-01T00:00:00+00:00"
    module.release_lock()


def test_release_lock_removes_only_own_lock(queue_runner) -> None:
    module = queue_runner
    assert module.acquire_lock() is True
    module.release_lock()
    # release_lock() unlocks the flock but must not delete a shared file path
    assert module.LOCK_PATH.exists()
    # Releasing again is a no-op.
    module.release_lock()


def test_lock_survives_after_ttl_without_process(queue_runner) -> None:
    """A stale lock with no running process can be taken over."""
    module = queue_runner
    module.MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text("2020-01-01T00:00:00+00:00", encoding="utf-8")

    with patch.object(module, "_lock_age_seconds", return_value=module.LOCK_TTL_SECONDS + 60):
        with patch.object(module, "any_matrix_process_running", return_value=False):
            acquired = module.acquire_lock()

    assert acquired is True


# ─── dedupe_executions ────────────────────────────────────────────────────────


def test_dedupe_rejects_unguarded_deletes_without_explicit_confirm() -> None:
    """Deleting requires an explicit --confirm flag; default is dry-run."""
    module = _load_script("dedupe_executions.py")

    with patch.object(module, "init_db"), patch.object(module, "get_session"):
        deleted = module.dedupe_executions()

    assert deleted == 0, "without confirm the script must not delete anything"


def test_dedupe_dry_run_reports_without_deleting() -> None:
    module = _load_script("dedupe_executions.py")

    with patch.object(module, "init_db"), patch.object(module, "get_session") as session_ctx:
        deleted = module.dedupe_executions(dry_run=True)

    assert deleted == 0
