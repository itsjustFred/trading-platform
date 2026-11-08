# Shared body of the gate runner. Sourced by scripts/gate-tests.sh, and sourced by
# tests/test_gate_tests_harness.py so the runner's own rules are tested rather than assumed.
#
# Why this is a library and not part of the script: the defect this exists to prevent cannot be
# tested through the script alone. A gate with no command used to be recorded as a pass, and the
# only way to pin that is to call `run_one` with no command - which the script can never do, because
# every call site there has one. Sourcing the same function the script uses is what makes the
# regression test exercise the real code instead of a copy of it.

gate_names=()
gate_results=()

# run_one NAME CMD...
#
# Records the gate under NAME and runs CMD. A gate with **no command** is recorded as failed:
# `if "$@"` with an empty argument list runs nothing, succeeds, and the original runner counted that
# as `ok`. That is how `gate-tests.sh` reported "ALL 13 GATE SUITES PASSED" while one of them had no
# command at all - its test file had been pasted under the following gate, so the suite ran but the
# gate that claimed it was empty. A gate that did not run is not a gate that passed.
run_one() {
    local name="$1"
    shift
    printf '\n=== %s\n' "$name"
    gate_names+=("$name")
    if [ "$#" -eq 0 ]; then
        printf '  NO COMMAND - a gate with nothing to run is not a gate\n' >&2
        gate_results+=("no-command")
        return 1
    fi
    if "$@"; then
        gate_results+=("ok")
    else
        local rc=$?
        gate_results+=("rc=$rc")
    fi
}

# The number of gates that did not pass. Prints it; never exits, so a caller can decide.
gate_failed_count() {
    local failed=0 i
    for i in "${!gate_results[@]}"; do
        if [ "${gate_results[$i]}" != "ok" ]; then
            failed=$((failed + 1))
        fi
    done
    printf '%s\n' "$failed"
}

# gate_roster_check EXPECTED_NAME...
#
# Compares the gates that were declared with the gates that ran, **by name**. Returns 2 and prints
# the difference when they disagree; 0 otherwise.
#
# By name and not by count, because the failure this guards against is a gate being renamed,
# swapped for a different command, or moved behind a conditional - all of which can keep the count
# identical. A count check would pass through every one of those.
gate_roster_check() {
    local expected=("$@")
    local missing extra
    missing="$(comm -23 <(printf '%s\n' "${expected[@]}" | sort) \
                        <(printf '%s\n' "${gate_names[@]}" | sort))"
    extra="$(comm -13 <(printf '%s\n' "${expected[@]}" | sort) \
                      <(printf '%s\n' "${gate_names[@]}" | sort))"
    if [ -z "$missing" ] && [ -z "$extra" ]; then
        return 0
    fi
    printf '  ROSTER MISMATCH\n' >&2
    if [ -n "$missing" ]; then
        printf '    declared but did not run: %s\n' "$(printf '%s' "$missing" | tr '\n' ' ')" >&2
    fi
    if [ -n "$extra" ]; then
        printf '    ran but was not declared: %s\n' "$(printf '%s' "$extra" | tr '\n' ' ')" >&2
    fi
    printf '  A gate that did not run is not a gate that passed, so the pass/fail count would\n' >&2
    printf '  describe a roster that does not exist.\n' >&2
    return 2
}
