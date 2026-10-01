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
#
# Usage:
#   mutate.sh --file F --old 'T' --new 'T' --label 'what this should break' \
#             [--suite 'pytest paths'] [--expect killed|survived]
#
# Exit codes:
#   0  applied, suite ran, verdict printed, and matched --expect if given
#   2  the mutation text was not uniquely present - NOTHING WAS MUTATED
#   3  applied but the file did not change - aborted
#   4  the suite did not run (infra failure) - no verdict
#   5  the verdict did not match --expect
#   6  the revert failed - working tree dirty, investigate immediately

set -o nounset
set -o pipefail

PYTHON="${PYTHON:-/home/ubuntu/Desktop/trading-platform/.venv/bin/python}"
FILE=""; OLD=""; NEW=""; LABEL=""; EXPECT=""
SUITE_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --file)   FILE="$2"; shift 2 ;;
    --old)    OLD="$2"; shift 2 ;;
    --new)    NEW="$2"; shift 2 ;;
    --label)  LABEL="$2"; shift 2 ;;
    --expect) EXPECT="$2"; shift 2 ;;
    --suite)  read -r -a SUITE_ARGS <<< "$2"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
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
if old and old != new and old in after:
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
if [ -z "$COLLECTED" ]; then
  printf '  %-52s INFRA - no tests were collected, no verdict\n' "$LABEL"
  printf '%s\n' "$OUT" | tail -2 | cut -c1-80 | sed 's/^/         /'
  exit 4
fi

VERDICT="SURVIVED"
[ "$RC" != "0" ] && VERDICT="KILLED"
FAILURES="$(printf '%s\n' "$OUT" | grep -cE '^FAILED' || true)"
SUMMARY="$(printf '%s\n' "$OUT" | tail -1 | cut -c1-52)"

printf '  %-52s %-9s rc=%s collected=%s failed=%s  %s\n' \
       "$LABEL" "$VERDICT" "$RC" "$COLLECTED" "$FAILURES" "$SUMMARY"
printf '%s\n' "$OUT" | grep -E '^FAILED' | head -2 | cut -c1-72 | sed 's/^/         /'

# Case-insensitive: the verdict is upper-case for the table, and an expectation
# written lower-case in a shell command should not fail on spelling alone.
EXPECT_LC="$(printf '%s' "$EXPECT" | tr '[:upper:]' '[:lower:]')"
VERDICT_LC="$(printf '%s' "$VERDICT" | tr '[:upper:]' '[:lower:]')"
if [ -n "$EXPECT" ] && [ "$EXPECT_LC" != "$VERDICT_LC" ]; then
  printf '         EXPECTED %s, GOT %s\n' "$EXPECT_LC" "$VERDICT_LC"
  exit 5
fi
exit 0