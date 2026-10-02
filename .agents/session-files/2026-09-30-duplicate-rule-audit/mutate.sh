#!/usr/bin/env bash
# A mutation harness that cannot print a verdict it has not verified.
#
# Written because an ad-hoc harness in this session reported SURVIVED for a
# mutation that 177 tests actually fail on. The mutation had applied and the
# suite really did fail, yet the harness printed "no test failed". Whatever the
# cause in that run, the answer is not to be more careful - it is to delete the
# paths by which an unverified verdict can be printed.
#
# Two of those paths are closed here:
#
#   1. A mutation that does not apply exits 2. That is reported as APPLY-FAILED,
#      never as SURVIVED - the two are indistinguishable from the outside, and
#      confusing them is exactly how a coverage gap gets invented.
#   2. A suite that fails to *run* (pytest usage error, no tests collected, a
#      path that does not exist) is reported as INFRA, never as KILLED. A
#      non-zero exit from a pytest that never collected anything is not evidence
#      that a mutation was caught, and the first version of this script made
#      precisely that mistake.
#   3. A non-zero exit from an UNRELATED failure is not evidence either. The
#      third version decided the verdict from `rc != 0` alone, so a verified
#      equivalent mutant was reported KILLED whenever any other test in the suite
#      happened to fail. Proven: with one deliberately-failing unrelated test in
#      the suite, a mutation that survives on its own printed KILLED.
#      `--detector` now names the test that *should* fail, and a non-zero exit
#      with no matching failure is reported KILLED-UNATTRIBUTED - which never
#      satisfies `--expect killed`, so a misattributed kill fails loudly.
#   4. A collection error in ONE file leaves the other files' tests collected,
#      so the run is half-successful and rc is non-zero. That is INFRA.
#
# Usage:
#   mutate.sh --file F --old 'T' --new 'T' --label 'what this should break' \
#             [--suite 'pytest paths'] [--expect killed|survived] \
#             [--detector 'substring of the test that should fail']
#
# Pass --detector whenever you know which test is meant to catch the mutation.
# Without it a KILLED is printed with `attributed=unknown`, which is honest but
# is not a guarantee: it means "some test failed", not "this mutation was caught".
#
# Exit codes:
#   0  applied, suite ran, verdict printed, and matched --expect if given
#   2  the mutation text was not uniquely present - NOTHING WAS MUTATED
#   3  applied but the file did not change - aborted
#   4  the suite did not run (infra failure) - no verdict
#   5  the verdict did not match --expect
#   6  the revert failed - working tree dirty, investigate immediately
#   7  KILLED-UNATTRIBUTED - a test failed but not the one that should have. The
#      verdict is WRONG, not merely unverified. Never masked by --expect.
#   8  KILLED with no --detector - some test failed, but nothing proves it was
#      this mutation. The verdict is UNVERIFIED. Never masked by --expect.
#
# rc=0 therefore means: the mutation applied, the suite ran, the file was
# reverted, and the printed verdict is backed by a named failing test (or the
# verdict was SURVIVED, which needs no attribution). Anything the harness cannot
# prove exits non-zero, so a caller that checks rc - and every caller in this
# audit does - is never handed a green result it should not trust.

set -o nounset
set -o pipefail

PYTHON="${PYTHON:-/home/ubuntu/Desktop/trading-platform/.venv/bin/python}"
FILE=""; OLD=""; NEW=""; LABEL=""; EXPECT=""; DETECTOR=""
SUITE_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --file)     FILE="$2"; shift 2 ;;
    --old)      OLD="$2"; shift 2 ;;
    --new)      NEW="$2"; shift 2 ;;
    --label)    LABEL="$2"; shift 2 ;;
    --expect)   EXPECT="$2"; shift 2 ;;
    --detector) DETECTOR="$2"; shift 2 ;;
    --suite)    read -r -a SUITE_ARGS <<< "$2"; shift 2 ;;
    -h|--help)  sed -n '2,46p' "$0"; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[ -n "$FILE" ] && [ -n "$OLD" ] && [ -n "$LABEL" ] || {
  printf 'usage: mutate.sh --file F --old T --new T --label L [--suite S] [--expect V]\n' >&2
  exit 2
}
[ -f "$FILE" ] || { printf 'no such file: %s\n' "$FILE" >&2; exit 2; }
[ "$#" -gt 0 ] || true

if [ "${#SUITE_ARGS[@]}" -eq 0 ]; then
  SUITE_ARGS=(tests)
fi

BEFORE="$(md5sum "$FILE" | cut -d' ' -f1)"

# --- apply, and prove it applied -------------------------------------------
APPLY_RC=0
"$PYTHON" - "$FILE" "$OLD" "$NEW" <<'PYEOF' || APPLY_RC=$?
import pathlib, sys

path = pathlib.Path(sys.argv[1])
old, new = sys.argv[2], sys.argv[3]
source = path.read_text(encoding="utf-8")
count = source.count(old)
if count != 1:
    raise SystemExit(f"APPLY-FAILED count={count} in {path}")
path.write_text(source.replace(old, new, 1), encoding="utf-8")
after = path.read_text(encoding="utf-8")
if new and new not in after:
    raise SystemExit("APPLY-FAILED: new text absent after write")
# Only meaningful when the replacement does not itself contain the original.
# Appending to a line (`x = f(a)` -> `x = f(a) or g()`) makes `new` a superstring
# of `old`, so "old still present" would be trivially true and would refuse a
# mutation that applied correctly. Found by its own guard on D6.
if old and old != new and old not in new and old in after:
    raise SystemExit("APPLY-FAILED: old text still present after write")
PYEOF

if [ "$APPLY_RC" -ne 0 ]; then
  printf '  %-52s APPLY-FAILED (nothing was mutated)\n' "$LABEL"
  git checkout -- "$FILE" 2>/dev/null || true
  exit 2
fi

AFTER="$(md5sum "$FILE" | cut -d' ' -f1)"
if [ "$OLD" != "$NEW" ] && [ "$BEFORE" = "$AFTER" ]; then
  printf '  %-52s ABORTED - applied but file unchanged\n' "$LABEL"
  git checkout -- "$FILE" 2>/dev/null || true
  exit 3
fi

# --- run the suite ---------------------------------------------------------
OUT="$(env -u BASE_URL -u PERMA_ALLOW_LIVE_VIEWER_WRITES \
        "$PYTHON" -m pytest -q -p no:cacheprovider "${SUITE_ARGS[@]}" 2>&1)"
RC=$?

# --- revert before interpreting anything ----------------------------------
git checkout -- "$FILE"
REVERTED="$(md5sum "$FILE" | cut -d' ' -f1)"
if [ "$BEFORE" != "$REVERTED" ]; then
  printf '  %-52s REVERT-FAILED - working tree is dirty\n' "$LABEL"
  exit 6
fi

# --- did the suite actually run? ------------------------------------------
COLLECTED="$(printf '%s\n' "$OUT" | grep -oE '[0-9]+ passed' | head -1)"
COLLECTED="${COLLECTED%% *}"
if printf '%s\n' "$OUT" | grep -qE '^ERROR: file or directory not found|^ERROR: .*no tests ran'; then
  printf '  %-52s INFRA - the suite did not run, no verdict\n' "$LABEL"
  printf '%s\n' "$OUT" | grep -E '^ERROR' | head -2 | cut -c1-80 | sed 's/^/         /'
  exit 4
fi
# A collection error in ONE file leaves the other files' tests collected, so the run is
# half-successful and rc is non-zero. That is INFRA, not KILLED: the non-zero exit says
# something was wrong, not that this mutation was caught.
if printf '%s\n' "$OUT" | grep -qE 'errors? during collection|^Interrupted:|INTERNALERROR'; then
  printf '  %-52s INFRA - collection error, no verdict\n' "$LABEL"
  printf '%s\n' "$OUT" | grep -E 'errors? during collection|^Interrupted:|INTERNALERROR' | head -2 | cut -c1-80 | sed 's/^/         /'
  exit 4
fi
if [ -z "$COLLECTED" ]; then
  printf '  %-52s INFRA - no tests were collected, no verdict\n' "$LABEL"
  printf '%s\n' "$OUT" | tail -2 | cut -c1-80 | sed 's/^/         /'
  exit 4
fi

# --- attribute the failure -------------------------------------------------
# A non-zero exit only says *some* test failed. Capturing every failing id lets a
# caller name the test that ought to have caught this, and lets the harness refuse
# to claim a kill it cannot attribute.
FAILING_IDS="$(printf '%s\n' "$OUT" | sed -nE 's/^FAILED ([^ ]+).*/\1/p')"
FAILURES="$(printf '%s' "$FAILING_IDS" | grep -c . || true)"
SUMMARY="$(printf '%s\n' "$OUT" | tail -1 | cut -c1-44)"

VERDICT="SURVIVED"
ATTRIBUTED="n/a"
if [ "$RC" != "0" ]; then
  if [ -z "$DETECTOR" ]; then
    VERDICT="KILLED"; ATTRIBUTED="unknown"
  elif printf '%s\n' "$FAILING_IDS" | grep -qF -- "$DETECTOR"; then
    VERDICT="KILLED"; ATTRIBUTED="yes"
  else
    VERDICT="KILLED-UNATTRIBUTED"; ATTRIBUTED="no"
  fi
fi

# --- is the code under test the code we think it is? -----------------------
# The venv has an EDITABLE INSTALL of /home/ubuntu/Desktop/trading-platform, which
# contains a SECOND, DIVERGENT copy of `live_runner` (114KB against 163KB here, different
# md5). Inside pytest, the rootdir prepend puts this worktree first and the right copy
# wins - verified. But a run rooted anywhere else, or any import that happens before
# pytest's prepend, silently gets the stale one. A mutation run that measured the wrong
# repo would report honest-looking KILLED verdicts about code nobody ships.
#
# So the harness resolves the package itself and refuses to continue if the import
# does not come from this worktree. Cheap, and it turns a silent hazard into a loud one.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PKG="${FILE%%/*}"
if [ "$PKG" != "$FILE" ] && [ -n "$PKG" ]; then
  # The worktree is put on PYTHONPATH explicitly: `python -c` resolves relative to the
  # caller's cwd, and a harness invoked from elsewhere would otherwise fail to import the
  # package it is about to mutate and report that as an import mismatch.
  #
  # `__file__` alone is not enough: a package with no `__init__.py` is a NAMESPACE package
  # whose `__file__` is None, so it falls back to the first `__path__` entry. The real
  # `live_runner` has an `__init__.py` and would have worked either way - found by the
  # self-check's sandbox, which deliberately has no `__init__.py`.
  IMPORTED="$(PYTHONPATH="$REPO_ROOT" "$PYTHON" -c "
import importlib, sys
m = importlib.import_module(sys.argv[1])
print(getattr(m, '__file__', None) or (list(getattr(m, '__path__', [])) or [''])[0])
" "$PKG" 2>/dev/null || true)"
  case "$IMPORTED" in
    "$REPO_ROOT"/*) ;;
    *)
      printf '  %-52s IMPORT-MISMATCH - not measuring this worktree\n' "$LABEL"
      printf '         expected %s under %s\n' "$PKG" "$REPO_ROOT"
      printf '         resolved %s\n' "${IMPORTED:-<not importable>}"
      printf '         a second, divergent copy of %s is installed in this venv, so a run\n' "$PKG"
      printf '         rooted elsewhere would mutate one repo and report on the other.\n'
      exit 9
      ;;
  esac
fi

printf '  %-52s %-19s rc=%s collected=%s failed=%s attr=%s  %s\n' \
       "$LABEL" "$VERDICT" "$RC" "$COLLECTED" "$FAILURES" "$ATTRIBUTED" "$SUMMARY"
printf '%s\n' "$OUT" | grep -E '^FAILED' | head -2 | cut -c1-72 | sed 's/^/         /'

# --- rc must mean the claim is proven -------------------------------------
# The whole point of this exit code is that a caller which checks it is not told
# a verdict holds when the harness has just said it does not. A printed word is
# not an exit status, and this script existed because a green summary was
# mistaken for a passing run.
if [ "$ATTRIBUTED" = "no" ]; then
  printf '         every failing test, since none matched --detector %s:\n' "$DETECTOR"
  printf '%s\n' "$FAILING_IDS" | head -8 | sed 's/^/           /'
  printf '         this mutation is NOT proven caught; treat it as SURVIVED.\n'
  printf '         rc=7: the verdict is wrong, not merely unverified.\n'
  exit 7
fi
if [ "$ATTRIBUTED" = "unknown" ]; then
  printf '         rc=8: some test failed, but nothing proves it was THIS mutation.\n'
  printf '         Pass --detector <test id substring> to make the verdict checkable.\n'
  exit 8
fi

# Case-insensitive: the verdict is upper-case for the table, and an expectation
# written lower-case in a shell command should not fail on spelling alone.
EXPECT_LC="$(printf '%s' "$EXPECT" | tr '[:upper:]' '[:lower:]')"
VERDICT_LC="$(printf '%s' "$VERDICT" | tr '[:upper:]' '[:lower:]')"
if [ -n "$EXPECT" ] && [ "$EXPECT_LC" != "$VERDICT_LC" ]; then
  printf '         EXPECTED %s, GOT %s\n' "$EXPECT_LC" "$VERDICT_LC"
  exit 5
fi
exit 0