#!/usr/bin/env bash
# Run the stage's opencode agent and turn its stream into the PR-comment
# body. This is the shared, single copy of what used to be three
# near-identical "Run X agent" step bodies; the per-stage differences (agent
# id, prompt, notice, error nouns, comment title/footer, whether the run
# step itself titles the comment) are the case table below.
#
# Usage: run_agent.sh <review|mutation|premortem>
#
# Environment (set by the calling workflow/job):
#   MODEL, VARIANT, OPENCODE_VERSION, SKILL_PATH, SKILL_SOURCE (via GITHUB_ENV),
#   PR_NUMBER, BASE_SHA, HEAD_SHA, RUNNER_TEMP, GITHUB_OUTPUT,
#   GITHUB_STEP_SUMMARY, NVIDIA_API_KEY, OPENCODE_DISABLE_PROJECT_CONFIG.
set -euo pipefail

stage="${1:?usage: run_agent.sh <review|mutation|premortem>}"
case "$stage" in
  review)
    agent_id="pr-review"
    agent_noun="review agent"
    result_noun="review result"
    output_noun="Review output"
    fail_noun="The review agent run failed."
    model_vars="REVIEW_MODEL / REVIEW_VARIANT"
    notice_label="Review rules"
    report_noun="review"
    model_error_suffix=""
    comment_title="# Code Review Action"
    footer_style="review"
    title_in_step="yes"
    ;;
  mutation)
    agent_id="pr-mutation"
    agent_noun="mutation-testing agent"
    result_noun="mutation-testing result"
    output_noun="Mutation output"
    fail_noun="The mutation-testing agent run failed."
    model_vars="MUTATION_MODEL / MUTATION_VARIANT"
    notice_label="Mutation-testing rules"
    report_noun="mutation-testing"
    model_error_suffix=""
    comment_title="# Mutation Testing Action"
    footer_style="mutation"
    title_in_step="no"
    ;;
  premortem)
    agent_id="pr-premortem"
    agent_noun="pre-mortem agent"
    result_noun="pre-mortem result"
    output_noun="Pre-mortem output"
    fail_noun="The pre-mortem agent run failed."
    model_vars="PREMORTEM_MODEL / PREMORTEM_VARIANT"
    notice_label="Pre-mortem rules"
    report_noun="pre-mortem"
    model_error_suffix=" The stage status is reported as \`error\`."
    comment_title="# Pre-Mortem Action"
    footer_style="premortem"
    title_in_step="yes"
    ;;
  *)
    echo "::error::run_agent.sh: unknown stage '$stage'"
    exit 1
    ;;
esac

AGENTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSE_VERDICT="$AGENTS_DIR/parse_verdict.py"
QUOTA_COMMENT="$AGENTS_DIR/quota_comment.py"

# Without the secret an `nvidia/...` model can only fail with a 401; say why.
if [[ -n "${MODEL:-}" && "${MODEL%%/*}" == "nvidia" && -z "${NVIDIA_API_KEY:-}" ]]; then
  echo "::error::MODEL is ${MODEL}, but the ACTIONS_NVIDIA_API_KEY secret is not set (Settings -> Secrets and variables -> Actions)."
  exit 1
fi
out="$RUNNER_TEMP/$stage/comment.md"
# Whose rules ran is the one thing the report itself cannot show, and it is the
# fallback's whole point: while the base branch has no skill, the PR's own copy
# runs. Name it in the log and in the comment footer (below), so a verdict
# produced by a PR's own rules is never indistinguishable from one the base
# produced. The pre-mortem copy does the same; a silent SKILL_SOURCE is what
# let the fallback grade a PR unnoticed.
# The quota path is one body reached from three places: the non-zero-exit
# classifier below, and the two zero-exit dead ends (no summary line, no
# verdict line) where the cap has been seen arriving on a zero exit -
# opencode exiting 0 on "Free usage limit reached" is exactly the run
# shape that used to reach "no summary line" and go red, the outcome
# the decision of 2026-11-21 forbids.
# Over Kilo's OpenAI-compatible gateway a 429 reaches opencode only as the bare
# "Provider returned error": opencode retries it ("retrying (attempt N)") and, once
# it gives up, ends on the same line a 400 ends on without any retry. Only the
# retries tell a rate limit from a broken request, so a run that ended on that line
# after retrying gets a rate-limit line the quota classifier recognises; a run that
# retried and then went on is left alone.
note_exhausted_retries() {
  local stream="$1"
  local final_line
  final_line=$(grep -v '^[[:space:]]*$' "$stream" | tail -n 1) || true
  if grep -q 'retrying (attempt' "$stream" && [[ "$final_line" == *'rror: '*'Provider returned error'* ]]; then
    echo 'rate limit: opencode gave up retrying a retryable provider error' >> "$stream"
  fi
}
# opencode repeats the provider's error on every retry line ("retrying
# (attempt N): ... Too Many Requests"), also when a later retry succeeds.
# A cap only counts once all retries are through, so the quota classifier
# reads stderr without those lines (<file>.final); the final error line
# stays, and so does note_exhausted_retries' synthetic line, which must
# therefore never contain "retrying (attempt".
drop_retry_lines() {
  local stream="$1"
  grep -v 'retrying (attempt' "$stream" > "$stream.final" || true
}
# The agent has a shell and its stdout becomes a public PR comment and a
# workflow artifact. An `nvidia/...` run holds the key in this step's env,
# so before anything is kept the captured streams are checked for it: a stream
# that carries the key fails the step, which uploads and posts nothing (the later
# steps only run on success), and the files are removed besides. Actions masks
# the secret in the log on its own.
refuse_leaked_key() {
  if [[ -n "${NVIDIA_API_KEY:-}" ]] \
    && grep -qF -- "$NVIDIA_API_KEY" "$out.raw" "$out.stderr" 2>/dev/null; then
    rm -f "$out" "$out.raw" "$out.stderr" "$out.stderr.final"
    echo "::error::The agent output contains the NVIDIA API key; nothing is posted or uploaded. Rotate ACTIONS_NVIDIA_API_KEY."
    exit 1
  fi
}
# A failed run's retries are over, so if its last stderr line is a retry line
# (opencode cut off mid-retry) that line is the final word on why it failed and
# goes back into the classifier's view. Only for a failed run: after a retry that
# cleared, the last stderr line can be that retry too, and it must not count.
keep_last_retry_line() {
  local stream="$1"
  local final_line
  final_line=$(grep -v '^[[:space:]]*$' "$stream" | tail -n 1) || true
  case "$final_line" in
    *'retrying (attempt'*) printf '%s\n' "$final_line" >> "$stream.final" ;;
    *) : ;;
  esac
}
# The transient class the classifiers below share: a cap, a rate limit, an
# exhausted quota, a region refusal or an unavailable model - infra, not a
# result. Deliberately not the misconfiguration class (a model or variant
# that cannot be resolved at all): that is a genuine failure.
QUOTA_GREP='usage limit|rate.?limit|quota|insufficient credits|limit reached|not available in your country|service unavailable|overloaded|sign up for free to continue|too many requests'
has_quota_signature() {
  grep -qiE "$QUOTA_GREP" "$@"
}
report_quota_skip() {
  echo "::warning::The ${agent_noun} could not run - the free model was capped, rate-limited, refused or unavailable (see the group above for the exact reason). That is a transient infra failure, not a ${result_noun}: not run: quota. Re-run this job after the quota resets."
  # The same report the probe-skip path posts, written here because the agent
  # job already ran: the comment is the PR's view either way, and the `quota`
  # output is what keeps the post job from mapping an empty verdict to `error`.
  python3 "$QUOTA_COMMENT" --stage "$stage" --when midrun > "$out"
  echo "quota=true" >> "$GITHUB_OUTPUT"
  if [[ "$stage" = "review" ]]; then
    cat "$out" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
}
case "$stage" in
  review)
    prompt="Review PR #${PR_NUMBER}. Base ${BASE_SHA}, head ${HEAD_SHA}. PR metadata: $RUNNER_TEMP/review/meta.json. Review rules loaded from: ${SKILL_SOURCE}."
    ;;
  mutation)
    prompt="Mutation-test PR #${PR_NUMBER}. Base ${BASE_SHA}, head ${HEAD_SHA}. PR metadata: $RUNNER_TEMP/mutation/meta.json. Scope file: $RUNNER_TEMP/mutation/scope.txt. Rules loaded from: ${SKILL_SOURCE}."
    ;;
  premortem)
    prompt="Pre-mortem PR #${PR_NUMBER}. Base ${BASE_SHA}, head ${HEAD_SHA}. PR metadata: $RUNNER_TEMP/premortem/meta.json. Rules loaded from: ${SKILL_SOURCE}."
    ;;
  *)
    echo "::error::run_agent.sh: unknown stage '$stage'"
    exit 1
    ;;
esac
echo "::notice::${notice_label} from: ${SKILL_SOURCE}."
# Captured through a variable, never a file the agent can reach, and
# stderr to a file so a failed run's reason stays greppable below.
if ! raw=$(opencode run --standalone --archived --agent "$agent_id" \
  "$prompt" 2> "$out.stderr"); then
  note_exhausted_retries "$out.stderr"
  drop_retry_lines "$out.stderr"
  keep_last_retry_line "$out.stderr"
  printf '%s\n' "$raw" > "$out.raw"
  cat "$out.stderr" >> "$out.raw"
  refuse_leaked_key
  # The reason the agent failed is only in stderr, and a file no one
  # reads is not a diagnosis: a failed run of this job has ended with
  # a bare "Process completed with exit code 1" and no cause, which
  # cannot tell a capped free model from a broken prompt. So the
  # captured output is echoed, bounded so a runaway agent cannot flood
  # the log, before the classification below reads it.
  echo "::group::Agent stdout and stderr (last 4000 bytes)"
  tail -c 4000 "$out.raw"
  echo "::endgroup::"
  # Two causes share this grep and need opposite remedies, so they are classified
  # separately. A cap, a rate limit, an exhausted quota or a region refusal is
  # transient infra, not a result: by the decision of 2026-11-21 the run
  # does not go red for it - the step writes the "not run: quota" comment and
  # exits 0 (the mutation stage's Finish comment step titles it like any
  # other), and the remedy is to re-run the job once the quota resets. A model
  # or variant that cannot be resolved at all is not transient: the run
  # cannot succeed until the repository variable changes, and telling an
  # operator to re-run it would send them into a loop. Only the first class
  # takes the quota path; the second fails with its own message, and a reason
  # in neither class (an `unauthorized` that could be either) is left to the
  # bare failure and the captured stderr above it, which is the reason itself -
  # better than guessing for it.
  if has_quota_signature "$out.stderr.final"; then
    report_quota_skip
  elif grep -qiE 'model (is )?(not (found|available|supported))|no provider|invalid model|unknown variant' "$out.stderr"; then
    echo "::error::The ${agent_noun} could not resolve its model - ${model_vars} name a model, variant or provider that does not exist or is not authorised (see the group above for the exact reason). That is a configuration error, not a transient one: re-running this job cannot succeed until the repository variable changes.${model_error_suffix}"
  else
    echo "::error::${fail_noun}"
  fi
  exit 1
fi
printf '%s\n' "$raw" > "$out.raw"
drop_retry_lines "$out.stderr"
refuse_leaked_key
# stderr went to a file for the failure classifier; on success it
# would otherwise never reach the log, so it is echoed bounded too.
if [[ -s "$out.stderr" ]]; then
  echo "::group::Agent stderr (last 4000 bytes)"
  tail -c 4000 "$out.stderr"
  echo "::endgroup::"
fi
# opencode prints every assistant message; keep only the final comment,
# which starts at the last "**Summary:**" line. Drop a closing code
# fence the model may have wrapped around it.
start=$(grep -n '^\*\*Summary:' "$out.raw" | tail -n 1 | cut -d: -f1 || true)
if [[ -z "$start" ]]; then
  # opencode has been seen exiting 0 on the cap, with the signature on
  # stdout: a zero exit is not a clean run, so this dead end is
  # classified before it fails. The grep covers the raw stream and
  # stderr both, and runs only here - a real report's own text may
  # mention "quota", so a produced report is never re-classified.
  if has_quota_signature "$out.raw" "$out.stderr.final"; then
    echo "::group::Agent output (last 4000 bytes)"
    tail -c 4000 "$out.raw"
    echo "::endgroup::"
    report_quota_skip
  fi
  echo "::error::${output_noun} has no summary line; not posting."
  cat "$out.raw"
  exit 1
fi
tail -n "+${start}" "$out.raw" | sed -e '${/^```[[:space:]]*$/d}' > "$out"
# The posted report is the tail of the agent's stream, and the anchor above is a
# heuristic: it takes the *last* line starting `**Summary:**` anywhere in the stream,
# so a later one at column 0 - the skill's own template quoted back inside a
# Mitigation block, say - moves the anchor down and the comment then starts at the
# quote instead of at the report. What *is* diagnosable has to be diagnosable without
# the stream: the raw output is uploaded with the comment below, and the byte counts
# are logged, so a comment that lost its opening sections is a run someone can
# reconstruct and read rather than one whose report silently changed shape.
kept=$(wc -c < "$out" | tr -d '[:space:]')
total=$(wc -c < "$out.raw" | tr -d '[:space:]')
echo "::notice::The ${report_noun} report is ${kept} of the agent's ${total} bytes of output; $((total - kept)) bytes of streamed messages and tool output are not posted (the raw output is an artifact of this run)."
# The verdict must be the last non-empty line (a verdict quoted from the
# diff elsewhere in the output does not count), and it must be one of the
# tokens the stage's skill documents - an exact-token match, so
# `Verdict: PASSed` and `Verdict: BLOCKED` are refused instead of matching
# as PASS and BLOCK. scripts/agents/parse_verdict.py is the single reader
# all three stages share; a sign-off note the model put on its own line
# after the verdict is tolerated there.
if ! verdict=$(python3 "$PARSE_VERDICT" verdict --stage "$stage" "$out" 2> /dev/null); then
  # Same zero-exit cap as above: a report that starts but never reaches
  # a valid verdict can still be the quota signature, on either stream.
  if has_quota_signature "$out.raw" "$out.stderr.final"; then
    echo "::group::Agent output (last 4000 bytes)"
    tail -c 4000 "$out.raw"
    echo "::endgroup::"
    report_quota_skip
  fi
  echo "::error::${output_noun} does not end with a valid verdict line; not posting."
  cat "$out.raw"
  exit 1
fi
if [[ "$stage" != "mutation" ]] \
  && { [[ "$(git rev-parse HEAD)" != "$HEAD_SHA" ]] || [[ -n "$(git status --porcelain)" ]]; }; then
  echo "::error::The ${agent_noun} modified the checkout; not posting."
  git status --porcelain
  exit 1
fi
case "$stage" in
  premortem)
    # A file, not $GITHUB_OUTPUT: the agent shares this step's
    # environment and could otherwise append its own verdict.
    printf '%s\n' "$verdict" > "$RUNNER_TEMP/$stage/verdict.txt"
    ;;
  *)
    : ;;
esac
case "$footer_style" in
  review)
    # Footer: the model, its variant and the skill the review rules came
    # from (the skill directory's name, not its path), plus whose rules
    # actually ran, so a reader can map the verdict to the rules that
    # produced it.
    skill_name=$(basename "$(dirname "$SKILL_PATH")")
    printf '\n\n---\n_Automated review using %s with `%s` skill (rules from %s)_\n' \
      "${MODEL#*/}${VARIANT:+ (${VARIANT})}" "$skill_name" "${SKILL_SOURCE}" >> "$out"
    ;;
  premortem)
    # Footer: which diff was analysed, then the model, its variant and the skill
    # the rules came from (the skill directory's name, not its path), so a
    # reader can map the verdict to the rules that produced it. The pair comes
    # first because it is the one thing that cannot be re-derived from the
    # comment: a verdict is current for its base...head and nothing else, and a
    # retarget, a base branch that moved or a head that moved on can each leave
    # this comment describing a diff nobody is looking at any more.
    skill_name=$(basename "$(dirname "$SKILL_PATH")")
    {
      printf '\n_Analysed base `%s` ... head `%s`._\n' "${BASE_SHA:0:8}" "${HEAD_SHA:0:8}"
      printf '\n---\n_Automated pre-mortem using %s with `%s` skill (rules from %s)_\n' \
        "${MODEL#*/}${VARIANT:+ (${VARIANT})}" "$skill_name" "${SKILL_SOURCE}"
    } >> "$out"
    ;;
  mutation)
    # Untitled and footerless here: the Finish comment step titles it and
    # appends the footer like any other mutation result (or the empty-scope
    # variant when no agent ran).
    ;;
  *)
    : ;;
esac
if [[ "$title_in_step" = "yes" ]]; then
  # Title first, so the comment is recognisable among the other bots'.
  { printf '%s\n\n' "$comment_title"; cat "$out"; } > "$out.titled"
  mv "$out.titled" "$out"
  cat "$out" >> "$GITHUB_STEP_SUMMARY"
fi
