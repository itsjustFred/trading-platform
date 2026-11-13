---
name: platform-pr-verify
description: Mandatory verification pipeline for every trading-platform pull request. Drives SonarCloud, the Agent Code Review, Agent Mutation Testing and Agent Pre-Mortem actions, and the CodeRabbit CLI in sequence, fixing findings and re-running until every stage passes without relevant findings. Use after pushing a PR, and when watching or babysitting one.
---

Use this guide as the verification pipeline for the PR. Most stages run in GitHub Actions; your job is to read their results, fix what they find, and run the last stage yourself.

## Stages

Each stage starts only after the previous one passed. Every push to the PR restarts the pipeline from stage 1 for the new head commit; results of older commits no longer count.

1. **SonarCloud** (check run `SonarCloud Code Analysis`, runs on each push). Passes when the quality gate is green. A failure can be acceptable (e.g. Cognitive Complexity too high on an unimportant function, or a rule the user explicitly accepted).
   - Read the PR comment, or list the issues with `sonar list issues --project itsjustFred_trading-platform --pull-request <N> --format toon`. With many issues the output gets large (PR #139: about 190k characters), so filter with `--severities` or redirect to a file and search it instead of reading it whole.
2. **Agent Code Review** (`.github/workflows/agent-code-review.yml`, commit status `agent/code-review`). Starts by itself once SonarCloud is green on the head commit. A read-only agent hunts logic errors first, then clarity, then spec and repo standards, and posts one comment titled `# Code Review Action`. Status: `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE, run failed, or not run (the description says why).
3. **Agent Mutation Testing** (`.github/workflows/agent-mutation-testing.yml`, commit status `agent/mutation-testing`). Dispatched by the review workflow when its verdict is PASS. Mutates the PR's changed production Python and posts one comment titled `# Mutation Testing Action` listing survived mutations with the test that would catch each. Status: `success` = PASS, `failure` = GAPS, `error` = INCOMPLETE, run failed, or not run.
4. **Agent Pre-Mortem** (`.github/workflows/agent-pre-mortem.yml`, commit status `agent/pre-mortem`). Dispatched by the mutation workflow when its verdict is PASS. A read-only agent writes the post-mortems of what this change could break later - wrong trade, wrong backtest result, silent failure, data loss - and posts one comment titled `# Pre-Mortem Action` listing ranked scenarios, each with its `file:line`, trigger, impact and mitigation. Status: `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE, run failed, or not run.
5. **CodeRabbit CLI** (you run it). Only after stages 1-4 passed. Info and trivial findings are allowed. The free tier allows 150 files per review and 3 reviews per hour; when rate-limited, the CLI shows the remaining time. Docs: https://docs.coderabbit.ai/cli

Read the state of the head commit with:

```
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/status --jq '.statuses[] | "\(.context): \(.state) - \(.description)"'
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/check-runs --jq '.check_runs[] | "\(.name): \(.status) \(.conclusion)"'
```

If you run in T3 Code, use the `watch_pull_request` tool instead of writing a watcher, polling or sleeping; it notifies you when the linked PR changes. Mutation testing can take up to 75 minutes, the pre-mortem up to 30.

## Fix loop

If a stage fails, fix the issue first, then let the pipeline re-run it; only after it passes, go to the next stage.

- **SonarCloud / Code Review:** fix each relevant finding in the PR (code, tests or docs), push, and wait for the pipeline to reach the stage again.
- **Mutation Testing:** a survived mutation means a missing test case. Add the test the comment proposes (or a better one) to the PR, check locally that it fails on the mutation and passes without it, push.
- **Pre-Mortem:** a Severe or Moderate scenario is a hardening task. Add the smallest change that stops it - the assertion, invariant, test or comment the scenario names as mitigation - to the PR, push, and wait for the pipeline to reach the stage again. A scenario judged implausible is an override, not a fix (see below); say why in a PR comment.
- **CodeRabbit:** fix the findings, push, and wait for stages 1-4 again before re-running the CLI.
- Collect the fixes for one stage into one push where you can: every push restarts all stages and costs Actions minutes.

The models in the Actions (named in each comment's footer) may be weaker than you. Check every finding against the code before acting on it: fix what is real, and do not dismiss a finding just because the model is small.

## Override

If a stage is red but every remaining finding is irrelevant (false positive, out of scope, accepted by the user), you may start the next stage by hand:

1. Post one PR comment headed `Pipeline override: <stage>` for the head commit. Give each finding its own line with the reason it does not apply. A blanket "findings irrelevant" is not enough; a human reads these reasons before merging.
2. Dispatch the next stage:
   - SonarCloud red -> `gh workflow run agent-code-review.yml --ref <pr-branch> -f pr_number=<N>` (a manual run skips the SonarCloud gate).
   - Code Review red -> `gh workflow run agent-mutation-testing.yml --ref <pr-branch> -f pr_number=<N>`.
   - Mutation Testing red -> `gh workflow run agent-pre-mortem.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>`.
   - Pre-Mortem red -> go on to the CodeRabbit CLI.

An override applies to the commit it was written for. After a new push, judge the new results again.

## Done

The loop is done when, on the current head commit, every stage passed or was overridden as above, and the CodeRabbit CLI has no findings beyond info or trivial. Then set the `ready-for-human` label. Any further change to the PR restarts the pipeline; remove `ready-for-human` while you work and re-add it once the pipeline passes again.
