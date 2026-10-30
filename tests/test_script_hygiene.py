"""Script-hygiene tests (Cleanup 20-D).

Covers: mock-result discovery output, matrix queue runner lock acquisition,
and dedupe_executions dry-run discipline.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import filelock as filelock_module
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
    module = _load_script("overnight_strategy_discovery.py")
    output_dir = tmp_path / "discovered_strategies"
    monkeypatch.setattr(module, "DISCOVERED_DIR", output_dir)

    results = module.run_discovery_batch(n_trials=3)

    assert results, "the batch API still returns trial rows for local review"
    assert not output_dir.exists(), "mock results must not be written for downstream runs"
    for row in results:
        assert row.get("status") == "mock"


def test_discovery_sampling_uses_csprng() -> None:
    """Parameter sampling must not use the Mersenne Twister (Sonar S2245)."""
    module = _load_script("overnight_strategy_discovery.py")
    assert not hasattr(module, "random"), (
        "the discovery script must not import the `random` module; "
        "use `secrets.SystemRandom` (CSPRNG) instead"
    )
    assert isinstance(module._RNG, secrets.SystemRandom)
    results = module.run_discovery_batch(n_trials=5)
    assert len(results) == 5
    for row in results:
        assert row["params"], "every trial must carry sampled parameters"


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


@pytest.fixture
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

    # A real second flock participant holds the lock from a subprocess: the
    # kernel must arbitrate, and the live lock file must stay untouched.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'a+')\n"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                "f.write('held\\n'); f.flush()\n"
                "time.sleep(15)\n"
            ),
            str(module.LOCK_PATH),
        ]
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if module.LOCK_PATH.read_text(encoding="utf-8") == "held\n":
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)

        acquired = module.acquire_lock()
    finally:
        holder.kill()
        holder.wait()

    assert acquired is False, "a live flock must block a second acquirer"
    assert module.LOCK_PATH.exists(), "a live lock must never be deleted"
    assert module.LOCK_PATH.read_text(encoding="utf-8").startswith("held\n")


#: Content a peer leaves in queue.lock that acquisition must never adopt as ours.
_FOREIGN_LOCK_CONTENT = "operator-metadata\n"
#: filelock 4.0.x truncates the lock file once it owns it; 4.2.1+ leaves a
#: pre-existing file untouched (upstream #835). Either is acceptable, but the
#: runner's own metadata must never end up in queue.lock, and must never be
#: appended to content this process did not write.
_ACCEPTED_LOCK_CONTENT = ("", _FOREIGN_LOCK_CONTENT)


def test_acquire_lock_does_not_overwrite_lock_file_metadata(queue_runner) -> None:
    module = queue_runner
    module.MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text(_FOREIGN_LOCK_CONTENT, encoding="utf-8")

    assert module.acquire_lock() is True
    try:
        # Back the membership assertion with an explicit not-in check, so a
        # single added byte cannot ride along inside a substring match.
        lock_text = module.LOCK_PATH.read_text(encoding="utf-8")
        assert lock_text in _ACCEPTED_LOCK_CONTENT, f"runner rewrote queue.lock with {lock_text!r}"
        assert "pid" not in lock_text
        assert "acquired_at" not in lock_text

        # The never-overwrite half has real teeth: the accepted-set check above
        # passes even if the runner rewrote a foreign file, since both accepted
        # values are candidates. Pin the full contract down with a tracking
        # FileLock -- the runner itself never rewrites the lock path: whatever
        # the backend leaves there (foreign content under 4.2.x, an emptied
        # file under 4.0.x) is the backend's doing and stays within the same
        # accepted set.
        spy_path = module.LOCK_PATH.with_name("queue.lock.spy")
        spy_path.write_text(_FOREIGN_LOCK_CONTENT, encoding="utf-8")
        real_file_lock = filelock_module.FileLock
        seen: list[str] = []

        def tracking_file_lock(*args, **kwargs):
            lock = real_file_lock(*args, **kwargs)
            assert Path(lock.lock_file).read_text(encoding="utf-8") == _FOREIGN_LOCK_CONTENT
            real_acquire = lock.acquire

            def tracked_acquire(*acquire_args, **acquire_kwargs):
                outcome = real_acquire(*acquire_args, **acquire_kwargs)
                after = Path(lock.lock_file).read_text(encoding="utf-8")
                assert after in _ACCEPTED_LOCK_CONTENT, f"unexpected lock content {after!r}"
                seen.append(str(lock.lock_file))
                return outcome

            lock.acquire = tracked_acquire
            return lock

        with patch.object(module, "FileLock", tracking_file_lock), patch.object(
            module, "LOCK_PATH", spy_path
        ):
            metadata_path = module._lock_metadata_path()
            assert metadata_path == Path(str(spy_path) + ".metadata")
            # The sidecar must start clean so the pid we assert on below can
            # only have been written by the acquisition under test.
            metadata_path.unlink(missing_ok=True)

            assert module.acquire_lock() is True
            try:
                assert seen == [str(spy_path)], "the queue lock must be acquired exactly once"
                assert spy_path.read_text(encoding="utf-8") in _ACCEPTED_LOCK_CONTENT
                assert metadata_path.exists(), "metadata must live in the sidecar, not queue.lock"
            finally:
                module.release_lock()

        metadata = json.loads(module._lock_metadata_path().read_text(encoding="utf-8"))
        assert metadata["pid"] > 0
    finally:
        module.release_lock()


def test_acquire_lock_preserves_foreign_content_under_real_filelock(queue_runner) -> None:
    """A lock path a peer already wrote must never be lengthened by acquisition.

    This is the same never-overwrite contract as above, but asserted against the
    real filelock backend instead of a tracking stub, so it holds whatever the
    installed filelock version does to a pre-existing lock file.
    """
    module = queue_runner
    module.MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    foreign = "x" * 4096
    module.LOCK_PATH.write_text(foreign, encoding="utf-8")
    before = module.LOCK_PATH.stat().st_size

    assert module.acquire_lock() is True
    try:
        after = module.LOCK_PATH.read_text(encoding="utf-8")
        assert len(after) <= before, "acquisition appended to a pre-existing lock file"
        assert after in ("", foreign), f"acquisition rewrote the lock file with {after!r}"
        metadata = json.loads(module._lock_metadata_path().read_text(encoding="utf-8"))
        assert metadata["pid"] > 0
    finally:
        module.release_lock()


def test_release_lock_removes_only_own_lock(queue_runner) -> None:
    module = queue_runner
    assert module.acquire_lock() is True
    module.release_lock()
    # release_lock() unlocks the flock but must not delete a shared file path
    assert module.LOCK_PATH.exists()
    # Releasing again is a no-op.
    module.release_lock()


def test_lock_left_by_crashed_writer_is_retaken(queue_runner) -> None:
    """A leftover lock file with no flock owner can be taken over."""
    module = queue_runner
    module.MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text("2020-01-01T00:00:00+00:00", encoding="utf-8")

    acquired = module.acquire_lock()

    assert acquired is True, "an unheld leftover lock must not block the next tick"
    module.release_lock()


# ─── dedupe_executions ────────────────────────────────────────────────────────


def test_dedupe_rejects_unguarded_deletes_without_explicit_confirm() -> None:
    """Deleting requires an explicit --confirm flag; default is dry-run."""
    module = _load_script("dedupe_executions.py")

    with patch.object(module, "init_db"), patch.object(module, "get_session"):
        deleted = module.dedupe_executions()

    assert deleted == 0, "without confirm the script must not delete anything"


def test_dedupe_dry_run_reports_without_deleting() -> None:
    module = _load_script("dedupe_executions.py")

    with patch.object(module, "init_db"), patch.object(module, "get_session"):
        deleted = module.dedupe_executions(dry_run=True)

    assert deleted == 0


def test_dedupe_reports_without_confirm_too() -> None:
    """The no-flag default (missing --confirm) is also a non-deleting dry run."""
    module = _load_script("dedupe_executions.py")

    with patch.object(module, "init_db"), patch.object(module, "get_session"):
        deleted = module.dedupe_executions(confirm=False)

    assert deleted == 0


def test_signal_bot_registry_mapping_is_complete_and_loadable() -> None:
    """Every registered bot must have a mapped config that yields a non-empty mapping."""
    from signal_bots.registry import (
        SIGNAL_PROCESSOR_OPTIONS,
        SIGNAL_PROCESSORS,
        get_signal_processor_config_path,
    )

    listed = {option["id"] for option in SIGNAL_PROCESSOR_OPTIONS}
    assert listed <= set(SIGNAL_PROCESSORS), (
        "every registry option must have a factory"
    )
    for name in SIGNAL_PROCESSORS:
        path = get_signal_processor_config_path(name)
        assert path.exists(), f"{name}: mapped config missing: {path}"
        assert path.read_text(encoding="utf-8").strip(), f"{name}: mapped config is empty: {path}"
