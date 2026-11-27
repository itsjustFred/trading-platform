#!/usr/bin/env bash
# Select which copy of the agent skill runs: the base branch's if it has one,
# otherwise (only while the skill is not merged yet) the PR's own copy. A base
# commit that cannot be read aborts the step before the `if`, at the
# standalone `git cat-file -e`: that check is the guard, because GitHub's
# default shell for a `run:` block is already `bash -e {0}` and fail-fast
# needs no help - and inside the `if` condition that follows, a failure is a
# false branch, not an abort. `set -euo pipefail` adds `pipefail` and `-u` (an
# unset variable now fails the step instead of expanding to nothing) and
# states the strictness, so it is not the guard and must not be read as one.
#
# The path lookup inside the `if` is a plain assignment rather than a command
# in a condition, so `errexit` does apply to it: `git ls-tree` is asked for
# the path and the *result* decides the branch, instead of `git cat-file`
# being run as the condition with its failure suppressed. The distinction
# matters: a suppressed failure inside a condition is only a false branch, so
# any `git` error - a missing object, an unreadable repository, a wrong
# revision syntax - fell through to the PR's own copy under a prompt that
# said the rules came from the base branch. Now only an empty listing (the
# base genuinely has no skill at that path) takes that branch, and every
# other failure stops the job.
#
# Usage: select_skill.sh <stage-dir> <missing-label>
#   stage-dir    the $RUNNER_TEMP subdirectory (review|mutation|premortem)
#   missing-label  e.g. "review skill" for the error when neither copy exists
set -euo pipefail

stage_dir="${1:?usage: select_skill.sh <stage-dir> <missing-label>}"
missing_label="${2:?usage: select_skill.sh <stage-dir> <missing-label>}"

mkdir -p "$RUNNER_TEMP/$stage_dir"
skill="$RUNNER_TEMP/$stage_dir/skill.md"
git cat-file -e "${BASE_SHA}^{commit}"
listing=$(git ls-tree "$BASE_SHA" -- "$SKILL_PATH")
if [ -n "$listing" ]; then
  git show "${BASE_SHA}:${SKILL_PATH}" > "$skill"
  echo "SKILL_SOURCE=base branch" >> "$GITHUB_ENV"
elif [ -f "$SKILL_PATH" ]; then
  cp "$SKILL_PATH" "$skill"
  echo "SKILL_SOURCE=this PR (not on the base branch yet)" >> "$GITHUB_ENV"
else
  echo "::error::No ${missing_label} found at base or PR head."
  exit 1
fi
