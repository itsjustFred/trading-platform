#!/usr/bin/env bash
# One entry point for the checks whose silent failure would make a green result a lie.
#
# Why this exists
# ---------------
# The gate runner this replaces lived outside the repository, in the agent harness. Its roster
# listed thirteen suites, and one of them had **no command**: its test file had been pasted under
# the following gate, so the suite really ran but the gate that claimed it ran nothing. `run_one`
# called `if "$@"` with an empty argument list, which runs nothing, succeeds, and was recorded as
# `ok`. The script then printed "ALL 13 GATE SUITES PASSED" - the most confident possible statement
# of a claim that was one-ninth empty.
#
# The roster was also never compared against what ran, so a suite that moved behind a conditional
# would have been reported as passed by the same mechanism, one level up.
#
# Two rules follow, and both are tested in tests/test_gate_tests_harness.py:
#   1. A gate with no command fails. It is not a pass and not a skip.
#   2. The roster is compared **by name** after the run. A gate that did not run, or one that ran
#      without being declared, exits 2 - distinct from 1, which means every gate ran and at least
#      one failed.
#
# The roster here is deliberately short. A gate nobody runs is not a gate, so only checks that are
# fast and that exist in this repository are on it. Adding a slow suite would make the runner the
# thing people skip.
#
# It is a **fast subset, not a replacement for the test suite**. CI runs the full pytest suite, the
# strategy suite, the packaging checks and these gates; this script answers "did the checks whose
# failure would be silent actually run" in seconds, which the full suite cannot do on a laptop
# before a commit. It is wired into `.github/workflows/ci.yml` for that reason.
#
# Usage:
#   scripts/gate-tests.sh                 # run every gate
#   scripts/gate-tests.sh --list          # print the declared roster, run nothing
#   PYTHON=/path/to/python scripts/gate-tests.sh
#
# Exit: 0 every gate ran and passed.
#       1 every gate ran and at least one failed.
#       2 the roster did not match, so the 0/1 verdict would describe gates that did not run.
#
# Read-only: it runs tests and a linter. It touches no database and no service.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# Not an absolute path to a specific venv: that would be a path this file cannot keep true. The
# caller picks the interpreter, and a missing dependency then fails the gate instead of skipping it.
PYTHON="${PYTHON:-python3}"

# shellcheck source=lib/gate_runner.sh
. "$HERE/lib/gate_runner.sh"

EXPECTED=(
    "script import origin guard"
    "output path guard"
    "test data-dir isolation guard"
    "gate harness self-check"
    "guarded scripts lint (ruff --select F)"
)

if [[ "${1:-}" = "--list" ]]; then
    printf '%s\n' "${EXPECTED[@]}"
    exit 0
fi

cd "$REPO_ROOT" || exit 1

# The guard that makes `python scripts/x.py` refuse to import `shared` from a different checkout.
# Without it a data-migrating script applies an older schema than the code believes and reports
# success - see tests/test_script_import_origin.py.
run_one "script import origin guard" \
    "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_script_import_origin.py

# The confinement every caller-supplied write path goes through. These scripts rewrite backtest
# JSON and harvest outputs; a path that climbs out of its root is a write into someone else's tree,
# and the failure would look like a successful run.
run_one "output path guard" \
    "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_safe_path.py

# The test suite's own isolation. `output path guard` above covers the operator scripts' write
# paths; it never observes what the *tests* write. Before this gate existed a plain `pytest` run
# pointed DATA_DIR at the repo's `data/` and wrote the real backtests.db, optimizations/ and a
# network-downloaded JForex SDK into it, and nothing failed. conftest now redirects DATA_DIR to a
# temp dir and fails the session if the repo `data/` changed; this pins that behaviour.
run_one "test data-dir isolation guard" \
    "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_data_dir_isolation.py

# The runner's own rules. The full-roster case is excluded here: it invokes this script, and a gate
# that runs the gate that runs it is a cycle, not a check.
run_one "gate harness self-check" \
    "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_gate_tests_harness.py \
    -k "not full_roster"

# The scripts that carry the import-origin guard. They mutate data, and their failure mode is
# silence, so an undefined name in one of them shows up as a half-finished migration rather than as
# an error. Scoped to this list rather than all of `scripts/` because the rest of that directory is
# not this slice's to change; a renamed or deleted file makes ruff exit 2, so the scope cannot
# shrink quietly.
GUARDED_SCRIPTS=(
    scripts/adaptive_harvester.py
    scripts/backtest_to_validation_ref.py
    scripts/dedupe_executions.py
    scripts/overnight_strategy_discovery.py
    scripts/recalculate_backtest_stats.py
    scripts/regenerate_backtests.py
    scripts/regenerate_mt5_parity_artifacts.py
    scripts/regenerate_qc_risk_parity_artifact.py
    scripts/run_risk_parity_backtest.py
    scripts/seed_demo_backtests.py
)

run_one "guarded scripts lint (ruff --select F)" \
    "$PYTHON" -m ruff check --select F "${GUARDED_SCRIPTS[@]}"

gate_roster_check "${EXPECTED[@]}"
roster_rc=$?
if [[ "$roster_rc" -ne 0 ]]; then
    printf '\n  Exiting 2 - the roster check failed, so the verdict below would be meaningless.\n'
    exit 2
fi

failed="$(gate_failed_count)"
if [[ "$failed" -eq 0 ]]; then
    printf '\n  ALL %s GATE SUITES PASSED\n' "${#gate_names[@]}"
    exit 0
fi
printf '\n  %s of %s GATE SUITES FAILED\n' "$failed" "${#gate_names[@]}"
exit 1
