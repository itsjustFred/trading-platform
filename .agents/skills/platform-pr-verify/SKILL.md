---
name: platform-pr-verify
description: Mandatory verification pipeline for every trading-platform pull request. Reads the SonarCloud check run, the four independent per-push Agent Actions (Code Review, Mutation Testing, Pre-Mortem, Cubic Review) and an on-demand CodeRabbit review, fixing findings and re-running until every stage passes without relevant findings. Use after pushing a PR, and when watching or babysitting one.
---

Use this guide as the verification pipeline for the PR. Every stage runs in GitHub Actions; your job is to read their results, fix what they find, and ask for the on-demand second opinion when it is needed.

## Stages

The four agent workflows are independent: each one runs by itself on every push to an open PR, none of them starts another, and each upserts one PR comment of its own. SonarCloud runs separately on each push as well. Every push re-runs them all on the new head commit; results of older commits no longer count. Fork PRs get no agent run, and the cubic gate skips drafts until the PR is ready for review.

1. **SonarCloud** (check run `SonarCloud Code Analysis`, runs on each push). Passes when the quality gate is green. A failure can be acceptable (e.g. Cognitive Complexity too high on an unimportant function, or a rule the user explicitly accepted).
   - Read the PR comment, or list the issues with `sonar list issues --project itsjustFred_trading-platform --pull-request <N> --format toon`. With many issues the output gets large (PR #139: about 190k characters), so filter with `--severities` or redirect to a file and search it instead of reading it whole.
2. **Agent Code Review** (`.github/workflows/agent-code-review.yml`). A read-only agent hunts logic errors first, then clarity, then spec and repo standards, and posts one comment titled `# Code Review Action` (updated in place on later pushes). It writes no commit status; the comment is the signal.
3. **Agent Mutation Testing** (`.github/workflows/agent-mutation-testing.yml`). Mutates the PR's changed production Python and posts one comment titled `# Mutation Testing Action` listing survived mutations with the test that would catch each. It writes no commit status either. A mutation that was left in place fails the run instead of posting.
4. **Agent Pre-Mortem** (`.github/workflows/agent-pre-mortem.yml`, commit status `agent/pre-mortem`). A read-only agent writes the post-mortems of what this change could break later - wrong trade, wrong backtest result, silent failure, data loss - and posts one comment titled `# Pre-Mortem Action` listing ranked scenarios, each with its `file:line`, trigger, impact and mitigation. On the analysed commit the status is `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE or the run failed (an infra failure, e.g. the free model's usage limit; re-run the job). A result for a commit that is no longer the head still lands its status on its own commit but leaves the newer comment alone. The status description names the PR it was written for, because `agent/pre-mortem` is one repo-global context: two open PRs sharing a head commit write the same context, and the description is what tells the two verdicts apart. A manual rerun names a `head_sha` that must be a full 40-hex SHA and one of that PR's own commits, and it runs beside the current head's run instead of cancelling it.
5. **Cubic** (`.github/workflows/agent-cubic-review.yml`, commit status `agent/cubic-review`). cubic.dev is a GitHub App that reviews every PR by itself, on open and on every push; the workflow gates on that review: it waits (up to 20 minutes) for cubic's review of the head commit, then classifies its findings by cubic's own priority: **P0/P1 block** the gate, **P2/P3 are advisory**. It posts one comment titled `# Cubic Review Action` with the counts and a link to the review. Status: `success` = reviewed, no P0/P1, or skipped because the PR is a draft; `failure` = at least one P0/P1; `error` = no cubic review of the head commit within the wait, a review body the classifier could not read, the run failed, or not run.
   - The inline comments are cubic's; each carries its priority in the first line (`P1: ...`) and a "Prompt for AI agents" block. Read them in the PR, not in the gate's comment.
   - The count the gate reports is cross-checked against cubic's own summary line (`**N issue(s) found**` / **No issues found**) in the `<!-- cubic:review-summary:start -->` block. A body with no readable summary, or a count that disagrees with the `P0:`-`P3:` tokens counted, is never a PASS: it is an `error` that names the mismatch, because an unread review is not a clean one.
   - Treat a **P0, P1 or P2 finding like a should-fix finding of the agent code review**: fix it in the PR, or answer it in its thread with why it does not apply and resolve the thread. The status mapping stays what it is - only P0/P1 turn the gate red - but a P2 (and a real P3) still has to be answered before the PR is ready for a human, and a real P2 belongs in the same push as the blocking findings.
   - Draft PRs: cubic does not review drafts, so on a draft head the gate writes `success` with the description "skipped: draft PR — cubic reviews only ready PRs" and waits for nothing. Marking the PR ready (`ready_for_review`) runs the gate again by itself, with a fresh wait on that head - the draft's `success` is not carried over.
   - A ready commit cubic never reviewed (rate limit, skipped run, a ready-for-review transition cubic does not act on) ends as `error`, not `failure`, with the re-run hint in the status description: re-trigger cubic from its review footer ("Re-trigger cubic"), then re-run the workflow by hand.

**CodeRabbit** is not a stage. Auto-review is off in the CodeRabbit dashboard; it is an on-demand second opinion. Trigger it with a PR comment `@coderabbitai review` only for:
- money-path or live-execution changes: `live_runner/`, broker adapters, order sizing/placement, risk gates, the live daemon, anything that can move money;
- a disagreement between cubic and the agent code review about the same finding, where a third opinion decides.

Read its result as advisory: fix real findings, and do not dismiss one just because another reviewer missed it.

Read the state of the head commit with:

```
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/status --jq '.statuses[] | "\(.context): \(.state) - \(.description)"'
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/check-runs --jq '.check_runs[] | "\(.name): \(.status) \(.conclusion)"'
```

The pre-mortem and the cubic gate write commit statuses; the code review and mutation stages present as comments. The bot comments of a PR:

```
gh api --paginate "repos/itsjustFred/trading-platform/issues/<N>/comments" --jq '.[] | select(.user.login == "github-actions[bot]") | "\(.id)  \(.body[:60])"'
```

If you run in T3 Code, use the `watch_pull_request` tool instead of writing a watcher, polling or sleeping; it notifies you when the linked PR changes. All four agent stages start on the same push and run in parallel: the mutation test takes up to 75 minutes, the pre-mortem up to 30, the cubic wait up to 20, so a CodeRabbit review usually waits for all of them.

## Fix loop

If a stage fails, fix the issue, push, and read the new result for the new head; only when every stage passed, trigger a CodeRabbit review if one is wanted.

- **SonarCloud / Code Review:** fix each relevant finding in the PR (code, tests or docs) and push. The stage re-runs on the new head.
- **Mutation Testing:** a survived mutation means a missing test case. Add the test the comment proposes (or a better one) to the PR, check locally that it fails on the mutation and passes without it, push.
- **Pre-Mortem:** a Severe or Moderate scenario is a hardening task. Add the smallest change that stops it - the assertion, invariant, test or comment the scenario names as mitigation - to the PR, push. A scenario judged implausible is an override, not a fix (see below); say why in a PR comment.
- **Cubic:** a P0/P1 finding is a blocking defect: fix it, push, and read the new result for the new head. If the finding does not apply, reply in its thread with the reason and resolve the thread; if that leaves the gate red, override it (below). P2/P3 need no action to pass the gate, but they are handled like review should-fixes - fix the real ones in the same push as the blocking findings.
- **CodeRabbit** (when you triggered it): fix the findings, push, and read every stage again.
- Collect the fixes into one push where you can: every push re-runs the four agent workflows and SonarCloud, and costs Actions minutes.

The models in the Actions (named in each comment's footer) may be weaker than you. Check every finding against the code before acting on it: fix what is real, and do not dismiss a finding just because the model is small.

## Override

A push is the normal re-run: every push re-runs the four agent workflows and SonarCloud on the new head, which is why nothing has to be started by hand after a fix. The one exception is a commit that will not be pushed again - an older head whose run failed for an infra reason - which only a manual dispatch can reach.

If a stage is red but every remaining finding is irrelevant (false positive, out of scope, accepted by the user), the stage may be recorded as overridden:

1. Post one PR comment headed `Pipeline override: <stage>` for the head commit. Give each finding its own line with the reason it does not apply. A blanket "findings irrelevant" is not enough; a human reads these reasons before merging.
2. An agent stage can also be re-run by hand for a specific commit, e.g. the pre-mortem after an infra failure, the cubic gate after a rate limit, or a commit whose run was missed:
   - `gh workflow run agent-pre-mortem.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>` (omit `head_sha` for the current head). The `head_sha` must be a full 40-hex SHA that is one of that PR's own commits; anything else is refused, so a rerun never analyses a foreign diff. A rerun of an older commit runs beside the current head's run - it does not cancel it - and its comment only lands while that commit is still the head.
   - `gh workflow run agent-cubic-review.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>` (omit `head_sha` for the current head). The cubic gate only ever grades the current head: a rerun that names an older commit is superseded and does nothing beyond closing what it marked pending.
   - The review and mutation workflows have no manual trigger; a push re-runs them.

An override applies to the commit it was written for. After a new push, judge the new results again.

## Done

The loop is done when, on the current head commit, every stage passed or was overridden as above, cubic has no unresolved P0/P1 finding, and any CodeRabbit review you triggered has no findings beyond info or trivial. Then set the `ready-for-human` label. Any further change to the PR re-runs the agent workflows; remove `ready-for-human` while you work and re-add it once the pipeline passes again.