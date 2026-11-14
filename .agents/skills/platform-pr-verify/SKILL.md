---
name: platform-pr-verify
description: Mandatory verification pipeline for every trading-platform pull request. Drives SonarCloud, the Agent Code Review, Agent Mutation Testing, Agent Pre-Mortem and the Cubic Review actions in sequence, fixing findings and re-running until every stage passes without relevant findings. CodeRabbit is an on-demand second opinion, not a stage. Use after pushing a PR, and when watching or babysitting one.
---

Use this guide as the verification pipeline for the PR. Every stage runs in GitHub Actions; your job is to read their results, fix what they find, and keep the chain moving.

## Stages

Each stage starts only after the previous one passed. Every push to the PR restarts the pipeline from stage 1 for the new head commit; results of older commits no longer count.

1. **SonarCloud** (check run `SonarCloud Code Analysis`, runs on each push). Passes when the quality gate is green. A failure can be acceptable (e.g. Cognitive Complexity too high on an unimportant function, or a rule the user explicitly accepted).
   - Read the PR comment, or list the issues with `sonar list issues --project itsjustFred_trading-platform --pull-request <N> --format toon`. With many issues the output gets large (PR #139: about 190k characters), so filter with `--severities` or redirect to a file and search it instead of reading it whole.
2. **Agent Code Review** (`.github/workflows/agent-code-review.yml`, commit status `agent/code-review`). Starts by itself once SonarCloud is green on the head commit. A read-only agent hunts logic errors first, then clarity, then spec and repo standards, and posts one comment titled `# Code Review Action`. Status: `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE, run failed, or not run (the description says why).
3. **Agent Mutation Testing** (`.github/workflows/agent-mutation-testing.yml`, commit status `agent/mutation-testing`). Dispatched by the review workflow when its verdict is PASS. Mutates the PR's changed production Python and posts one comment titled `# Mutation Testing Action` listing survived mutations with the test that would catch each. Status: `success` = PASS, `failure` = GAPS, `error` = INCOMPLETE, run failed, or not run.
4. **Agent Pre-Mortem** (`.github/workflows/agent-pre-mortem.yml`, commit status `agent/pre-mortem`). Dispatched by the mutation workflow when its verdict is PASS. A read-only agent writes the post-mortems of what this change could break later - wrong trade, wrong backtest result, silent failure, data loss - and posts one comment titled `# Pre-Mortem Action` listing ranked scenarios, each with its `file:line`, trigger, impact and mitigation. Status: `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE, run failed, or not run.
5. **Cubic** (`.github/workflows/agent-cubic-review.yml`, commit status `agent/cubic-review`). Dispatched by the pre-mortem workflow when its verdict is PASS. cubic.dev is a GitHub App that reviews every PR by itself, on open and on every push; the stage waits (up to 20 minutes) for cubic's review of the head commit, then classifies its findings by cubic's own priority: **P0/P1 block** the stage, **P2/P3 are advisory**. It posts one comment titled `# Cubic Review Action` with the counts and a link to the review. Status: `success` = reviewed, no P0/P1, or skipped because the PR is a draft; `failure` = at least one P0/P1; `error` = no cubic review of the head commit within the wait, the run failed, or not run.
   - The inline comments are cubic's; each carries its priority in the first line (`P1: ...`) and a "Prompt for AI agents" block. Read them in the PR, not in the stage comment.
   - Draft PRs: cubic does not review drafts, so on a draft head the stage writes `success` with the description "skipped: draft PR — cubic reviews only ready PRs" and waits for nothing. Marking the PR ready (`ready_for_review`) runs the stage again by itself, but only once the pre-mortem has passed on that head; it does not re-run the earlier stages.
   - A ready commit cubic never reviewed (rate limit, skipped run) ends as `error`, not `failure`: re-trigger cubic from its review footer ("Re-trigger cubic"), then re-dispatch the stage.

**CodeRabbit** is no longer a stage. Auto-review is off in the CodeRabbit dashboard; it is an on-demand second opinion. Trigger it with a PR comment `@coderabbitai review` only for:
- money-path or live-execution changes: `live_runner/`, broker adapters, order sizing/placement, risk gates, the live daemon, anything that can move money;
- a disagreement between cubic and the agent code review about the same finding, where a third opinion decides.

Read its result as advisory: fix real findings, and do not dismiss one just because another reviewer missed it.

Read the state of the head commit with:

```
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/status --jq '.statuses[] | "\(.context): \(.state) - \(.description)"'
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/check-runs --jq '.check_runs[] | "\(.name): \(.status) \(.conclusion)"'
```

If you run in T3 Code, use the `watch_pull_request` tool instead of writing a watcher, polling or sleeping; it notifies you when the linked PR changes. Mutation testing can take up to 75 minutes, the pre-mortem up to 30, the cubic wait up to 20.

## Fix loop

If a stage fails, fix the issue first, then let the pipeline re-run it; only after it passes, go to the next stage.

- **SonarCloud / Code Review:** fix each relevant finding in the PR (code, tests or docs), push, and wait for the pipeline to reach the stage again.
- **Mutation Testing:** a survived mutation means a missing test case. Add the test the comment proposes (or a better one) to the PR, check locally that it fails on the mutation and passes without it, push.
- **Pre-Mortem:** a Severe or Moderate scenario is a hardening task. Add the smallest change that stops it - the assertion, invariant, test or comment the scenario names as mitigation - to the PR, push, and wait for the pipeline to reach the stage again. A scenario judged implausible is an override, not a fix (see below); say why in a PR comment.
- **Cubic:** a P0/P1 finding is a blocking defect: fix it, push, and wait for the pipeline to reach the stage again. If the finding does not apply, reply in its thread with the reason and resolve the thread; if that leaves the stage red, override it (below). P2/P3 need no action to pass, but fix the real ones in the same push as the blocking findings.
- **CodeRabbit** (when you triggered it): fix the findings, push, and wait for the chain to pass again; there is no CodeRabbit status to wait for.
- Collect the fixes for one stage into one push where you can: every push restarts all stages and costs Actions minutes.

The models in the Actions (named in each comment's footer) may be weaker than you. Check every finding against the code before acting on it: fix what is real, and do not dismiss a finding just because the model is small.

## Override

If a stage is red but every remaining finding is irrelevant (false positive, out of scope, accepted by the user), you may start the next stage by hand:

1. Post one PR comment headed `Pipeline override: <stage>` for the head commit. Give each finding its own line with the reason it does not apply. A blanket "findings irrelevant" is not enough; a human reads these reasons before merging.
2. Dispatch the next stage:
   - SonarCloud red -> `gh workflow run agent-code-review.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>` (a manual run skips the SonarCloud gate).
   - Code Review red -> `gh workflow run agent-mutation-testing.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>`.
   - Mutation Testing red -> `gh workflow run agent-pre-mortem.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>`.
   - Pre-Mortem red -> `gh workflow run agent-cubic-review.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>`.
   - Cubic red -> nothing runs after it; the override comment is the record, and the `ready-for-human` label carries the decision to the human.

An override applies to the commit it was written for. After a new push, judge the new results again.

## Done

The loop is done when, on the current head commit, every stage passed or was overridden as above, cubic has no P0/P1 finding left unresolved, and any CodeRabbit run you triggered has no findings beyond info or trivial. Then set the `ready-for-human` label. Any further change to the PR restarts the pipeline; remove `ready-for-human` while you work and re-add it once the pipeline passes again.
