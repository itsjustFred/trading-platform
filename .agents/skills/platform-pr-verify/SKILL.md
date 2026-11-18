---
name: platform-pr-verify
description: Mandatory verification pipeline for every trading-platform pull request. Reads the SonarCloud check run, the three independent per-push Agent Actions (Code Review, Mutation Testing, Pre-Mortem) and the CodeRabbit CLI results, fixing findings and re-running until every stage passes without relevant findings. Use after pushing a PR, and when watching or babysitting one.
---

Use this guide as the verification pipeline for the PR. Most stages run in GitHub Actions; your job is to read their results, fix what they find, and run the last stage yourself.

## Stages

The three agent workflows are independent: each one runs by itself on every push to an open PR, none of them starts another, and each upserts one PR comment of its own. SonarCloud runs separately on each push as well. Every push re-runs them all on the new head commit; results of older commits no longer count. Fork PRs get no agent run, and the pre-mortem skips drafts until the PR is ready for review.

1. **SonarCloud** (check run `SonarCloud Code Analysis`, runs on each push). Passes when the quality gate is green. A failure can be acceptable (e.g. Cognitive Complexity too high on an unimportant function, or a rule the user explicitly accepted).
   - Read the PR comment, or list the issues with `sonar list issues --project itsjustFred_trading-platform --pull-request <N> --format toon`. With many issues the output gets large (PR #139: about 190k characters), so filter with `--severities` or redirect to a file and search it instead of reading it whole.
2. **Agent Code Review** (`.github/workflows/agent-code-review.yml`). A read-only agent hunts logic errors first, then clarity, then spec and repo standards, and posts one comment titled `# Code Review Action` (updated in place on later pushes). It writes no commit status; the comment is the signal.
3. **Agent Mutation Testing** (`.github/workflows/agent-mutation-testing.yml`). Mutates the PR's changed production Python and posts one comment titled `# Mutation Testing Action` listing survived mutations with the test that would catch each. It writes no commit status either. A mutation that was left in place fails the run instead of posting.
4. **Agent Pre-Mortem** (`.github/workflows/agent-pre-mortem.yml`, commit status `agent/pre-mortem`). A read-only agent writes the post-mortems of what this change could break later - wrong trade, wrong backtest result, silent failure, data loss - and posts one comment titled `# Pre-Mortem Action` listing ranked scenarios, each with its `file:line`, trigger, impact and mitigation. On the analysed commit the status is `success` = PASS, `failure` = BLOCK or CHANGES-REQUESTED, `error` = INCOMPLETE or the run failed (an infra failure, e.g. the free model's usage limit; re-run the job). A result for a commit that is no longer the head still lands its status on its own commit but leaves the newer comment alone.
5. **CodeRabbit CLI** (you run it). Only after stages 1-4 all passed on the head commit. Info and trivial findings are allowed. The free tier allows 150 files per review and 3 reviews per hour; when rate-limited, the CLI shows the remaining time. Docs: https://docs.coderabbit.ai/cli

Read the state of the head commit with:

```
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/status --jq '.statuses[] | "\(.context): \(.state) - \(.description)"'
gh api repos/itsjustFred/trading-platform/commits/<head-sha>/check-runs --jq '.check_runs[] | "\(.name): \(.status) \(.conclusion)"'
```

Only the pre-mortem writes a commit status; the review and mutation stages present as comments. The bot comments of a PR:

```
gh api --paginate "repos/itsjustFred/trading-platform/issues/<N>/comments" --jq '.[] | select(.user.login == "github-actions[bot]") | "\(.id)  \(.body[:60])"'
```

If you run in T3 Code, use the `watch_pull_request` tool instead of writing a watcher, polling or sleeping; it notifies you when the linked PR changes. All three agent stages start on the same push and run in parallel: the mutation test takes up to 75 minutes, the pre-mortem up to 30, so CodeRabbit usually waits for both.

## Fix loop

If a stage fails, fix the issue, push, and read the new result for the new head; only when every stage passed, go on to the CodeRabbit CLI.

- **SonarCloud / Code Review:** fix each relevant finding in the PR (code, tests or docs) and push. The stage re-runs on the new head.
- **Mutation Testing:** a survived mutation means a missing test case. Add the test the comment proposes (or a better one) to the PR, check locally that it fails on the mutation and passes without it, push.
- **Pre-Mortem:** a Severe or Moderate scenario is a hardening task. Add the smallest change that stops it - the assertion, invariant, test or comment the scenario names as mitigation - to the PR, push. A scenario judged implausible is an override, not a fix (see below); say why in a PR comment.
- **CodeRabbit:** fix the findings, push, and read all four stages again before re-running the CLI.
- Collect the fixes into one push where you can: every push re-runs the three agent workflows and SonarCloud, and costs Actions minutes.

The models in the Actions (named in each comment's footer) may be weaker than you. Check every finding against the code before acting on it: fix what is real, and do not dismiss a finding just because the model is small.

## Override

If a stage is red but every remaining finding is irrelevant (false positive, out of scope, accepted by the user), the stage may be recorded as overridden - nothing needs to be started by hand, since every push re-runs all of them:

1. Post one PR comment headed `Pipeline override: <stage>` for the head commit. Give each finding its own line with the reason it does not apply. A blanket "findings irrelevant" is not enough; a human reads these reasons before merging.
2. An agent stage can also be re-run by hand for a specific commit, e.g. the pre-mortem after an infra failure, or for a commit whose run was missed:
   - `gh workflow run agent-pre-mortem.yml --ref <pr-branch> -f pr_number=<N> -f head_sha=<head-sha>` (omit `head_sha` for the current head).
   - The review and mutation workflows have no manual trigger; a push re-runs them.

An override applies to the commit it was written for. After a new push, judge the new results again.

## Done

The loop is done when, on the current head commit, every stage passed or was overridden as above, and the CodeRabbit CLI has no findings beyond info or trivial. Then set the `ready-for-human` label. Any further change to the PR re-runs the agent workflows; remove `ready-for-human` while you work and re-add it once the pipeline passes again.